"""
homeassistant.components.httpinterface
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

This module provides an API and a HTTP interface for debug purposes.

By default it will run on port 8123.

All API calls have to be accompanied by an 'api_password' parameter and will
return JSON. If successful calls will return status code 200 or 201.

Other status codes that can occur are:
 - 400 (Bad Request)
 - 401 (Unauthorized)
 - 404 (Not Found)
 - 405 (Method not allowed)

The api supports the following actions:

/api - GET
Returns message if API is up and running.
Example result:
{
  "message": "API running."
}

/api/states - GET
Returns a list of entities for which a state is available
Example result:
[
    { .. state object .. },
    { .. state object .. }
]

/api/states/<entity_id> - GET
Returns the current state from an entity
Example result:
{
    "attributes": {
        "next_rising": "07:04:15 29-10-2013",
        "next_setting": "18:00:31 29-10-2013"
    },
    "entity_id": "weather.sun",
    "last_changed": "23:24:33 28-10-2013",
    "state": "below_horizon"
}

/api/states/<entity_id> - POST
Updates the current state of an entity. Returns status code 201 if successful
with location header of updated resource and as body the new state.
parameter: new_state - string
optional parameter: attributes - JSON encoded object
Example result:
{
    "attributes": {
        "next_rising": "07:04:15 29-10-2013",
        "next_setting": "18:00:31 29-10-2013"
    },
    "entity_id": "weather.sun",
    "last_changed": "23:24:33 28-10-2013",
    "state": "below_horizon"
}

/api/events/<event_type> - POST
Fires an event with event_type
optional parameter: event_data - JSON encoded object
Example result:
{
    "message": "Event download_file fired."
}

"""

import json
import threading
import logging
import time
import gzip
import os
import random
import string
from datetime import timedelta
from homeassistant.util import Throttle
from http.server import SimpleHTTPRequestHandler, HTTPServer
from http import cookies
from socketserver import ThreadingMixIn
from urllib.parse import urlparse, parse_qs

import homeassistant as ha
from homeassistant.const import (
    SERVER_PORT, CONTENT_TYPE_JSON,
    HTTP_HEADER_HA_AUTH, HTTP_HEADER_CONTENT_TYPE, HTTP_HEADER_ACCEPT_ENCODING,
    HTTP_HEADER_CONTENT_ENCODING, HTTP_HEADER_VARY, HTTP_HEADER_CONTENT_LENGTH,
    HTTP_HEADER_CACHE_CONTROL, HTTP_HEADER_EXPIRES, HTTP_OK, HTTP_UNAUTHORIZED,
    HTTP_NOT_FOUND, HTTP_METHOD_NOT_ALLOWED, HTTP_UNPROCESSABLE_ENTITY)
import homeassistant.remote as rem
import homeassistant.util as util
import homeassistant.util.dt as date_util
import homeassistant.bootstrap as bootstrap

DOMAIN = "http"
DEPENDENCIES = []

CONF_API_PASSWORD = "api_password"
CONF_SERVER_HOST = "server_host"
CONF_SERVER_PORT = "server_port"
CONF_DEVELOPMENT = "development"
CONF_SESSIONS_ENABLED = "sessions_enabled"

DATA_API_PASSWORD = 'api_password'

# Throttling time in seconds for expired sessions check
MIN_SEC_SESSION_CLEARING = timedelta(seconds=20)
SESSION_TIMEOUT_SECONDS = 1800
SESSION_KEY = 'sessionId'

_LOGGER = logging.getLogger(__name__)


def setup(hass, config=None):
    """ Sets up the HTTP API and debug interface. """
    if config is None or DOMAIN not in config:
        config = {DOMAIN: {}}

    api_password = util.convert(config[DOMAIN].get(CONF_API_PASSWORD), str)

    no_password_set = api_password is None

    if no_password_set:
        api_password = util.get_random_string()

    # If no server host is given, accept all incoming requests
    server_host = config[DOMAIN].get(CONF_SERVER_HOST, '0.0.0.0')

    server_port = config[DOMAIN].get(CONF_SERVER_PORT, SERVER_PORT)

    development = str(config[DOMAIN].get(CONF_DEVELOPMENT, "")) == "1"

    sessions_enabled = config[DOMAIN].get(CONF_SESSIONS_ENABLED, True)

    try:
        server = HomeAssistantHTTPServer(
            (server_host, server_port), RequestHandler, hass, api_password,
            development, no_password_set, sessions_enabled)
    except OSError:
        # Happens if address already in use
        _LOGGER.exception("Error setting up HTTP server")
        return False

    hass.bus.listen_once(
        ha.EVENT_HOMEASSISTANT_START,
        lambda event:
        threading.Thread(target=server.start, daemon=True).start())

    hass.http = server
    hass.config.api = rem.API(util.get_local_ip(), api_password, server_port)

    return True


# pylint: disable=too-many-instance-attributes
class HomeAssistantHTTPServer(ThreadingMixIn, HTTPServer):
    """ Handle HTTP requests in a threaded fashion. """
    # pylint: disable=too-few-public-methods

    allow_reuse_address = True
    daemon_threads = True

    # pylint: disable=too-many-arguments
    def __init__(self, server_address, request_handler_class,
                 hass, api_password, development, no_password_set,
                 sessions_enabled):
        super().__init__(server_address, request_handler_class)

        self.server_address = server_address
        self.hass = hass
        self.api_password = api_password
        self.development = development
        self.no_password_set = no_password_set
        self.paths = []
        self.sessions = SessionStore(sessions_enabled)

        # We will lazy init this one if needed
        self.event_forwarder = None

        if development:
            _LOGGER.info("running http in development mode")

    def start(self):
        """ Starts the HTTP server. """
        def stop_http(event):
            """ Stops the HTTP server. """
            self.shutdown()

        self.hass.bus.listen_once(ha.EVENT_HOMEASSISTANT_STOP, stop_http)

        _LOGGER.info(
            "Starting web interface at http://%s:%d", *self.server_address)

        # 31-1-2015: Refactored frontend/api components out of this component
        # To prevent stuff from breaking, load the two extracted components
        bootstrap.setup_component(self.hass, 'api')
        bootstrap.setup_component(self.hass, 'frontend')

        self.serve_forever()

    def register_path(self, method, url, callback, require_auth=True):
        """ Registers a path wit the server. """
        self.paths.append((method, url, callback, require_auth))


# pylint: disable=too-many-public-methods,too-many-locals
class RequestHandler(SimpleHTTPRequestHandler):
    """
    Handles incoming HTTP requests

    We extend from SimpleHTTPRequestHandler instead of Base so we
    can use the guess content type methods.
    """

    server_version = "HomeAssistant/1.0"

    def __init__(self, req, client_addr, server):
        """ Contructor, call the base constructor and set up session """
        self._session = None
        SimpleHTTPRequestHandler.__init__(self, req, client_addr, server)

    def _handle_request(self, method):  # pylint: disable=too-many-branches
        """ Does some common checks and calls appropriate method. """
        url = urlparse(self.path)

        # Read query input
        data = parse_qs(url.query)

        # parse_qs gives a list for each value, take the latest element
        for key in data:
            data[key] = data[key][-1]

        # Did we get post input ?
        content_length = int(self.headers.get(HTTP_HEADER_CONTENT_LENGTH, 0))

        if content_length:
            body_content = self.rfile.read(content_length).decode("UTF-8")

            try:
                data.update(json.loads(body_content))
            except (TypeError, ValueError):
                # TypeError if JSON object is not a dict
                # ValueError if we could not parse JSON
                _LOGGER.exception(
                    "Exception parsing JSON: %s", body_content)
                self.write_json_message(
                    "Error parsing JSON", HTTP_UNPROCESSABLE_ENTITY)
                return

        self._session = self.get_session()
        if self.server.no_password_set:
            api_password = self.server.api_password
        else:
            api_password = self.headers.get(HTTP_HEADER_HA_AUTH)

            if not api_password and DATA_API_PASSWORD in data:
                api_password = data[DATA_API_PASSWORD]

            if not api_password and self._session is not None:
                api_password = self._session.cookie_values.get(
                    CONF_API_PASSWORD)

        if '_METHOD' in data:
            method = data.pop('_METHOD')

        # Var to keep track if we found a path that matched a handler but
        # the method was different
        path_matched_but_not_method = False

        # Var to hold the handler for this path and method if found
        handle_request_method = False
        require_auth = True

        # Check every handler to find matching result
        for t_method, t_path, t_handler, t_auth in self.server.paths:
            # we either do string-comparison or regular expression matching
            # pylint: disable=maybe-no-member
            if isinstance(t_path, str):
                path_match = url.path == t_path
            else:
                path_match = t_path.match(url.path)

            if path_match and method == t_method:
                # Call the method
                handle_request_method = t_handler
                require_auth = t_auth
                break

            elif path_match:
                path_matched_but_not_method = True

        # Did we find a handler for the incoming request?
        if handle_request_method:

            # For some calls we need a valid password
            if require_auth and api_password != self.server.api_password:
                self.write_json_message(
                    "API password missing or incorrect.", HTTP_UNAUTHORIZED)

            else:
                if self._session is None and require_auth:
                    self._session = self.server.sessions.create(
                        api_password)

                handle_request_method(self, path_match, data)

        elif path_matched_but_not_method:
            self.send_response(HTTP_METHOD_NOT_ALLOWED)
            self.end_headers()

        else:
            self.send_response(HTTP_NOT_FOUND)
            self.end_headers()

    def do_HEAD(self):  # pylint: disable=invalid-name
        """ HEAD request handler. """
        self._handle_request('HEAD')

    def do_GET(self):  # pylint: disable=invalid-name
        """ GET request handler. """
        self._handle_request('GET')

    def do_POST(self):  # pylint: disable=invalid-name
        """ POST request handler. """
        self._handle_request('POST')

    def do_PUT(self):  # pylint: disable=invalid-name
        """ PUT request handler. """
        self._handle_request('PUT')

    def do_DELETE(self):  # pylint: disable=invalid-name
        """ DELETE request handler. """
        self._handle_request('DELETE')

    def write_json_message(self, message, status_code=HTTP_OK):
        """ Helper method to return a message to the caller. """
        self.write_json({'message': message}, status_code=status_code)

    def write_json(self, data=None, status_code=HTTP_OK, location=None):
        """ Helper method to return JSON to the caller. """
        self.send_response(status_code)
        self.send_header(HTTP_HEADER_CONTENT_TYPE, CONTENT_TYPE_JSON)

        if location:
            self.send_header('Location', location)

        self.set_session_cookie_header()

        self.end_headers()

        if data is not None:
            self.wfile.write(
                json.dumps(data, indent=4, sort_keys=True,
                           cls=rem.JSONEncoder).encode("UTF-8"))

    def write_file(self, path):
        """ Returns a file to the user. """
        try:
            with open(path, 'rb') as inp:
                self.write_file_pointer(self.guess_type(path), inp)

        except IOError:
            self.send_response(HTTP_NOT_FOUND)
            self.end_headers()
            _LOGGER.exception("Unable to serve %s", path)

    def write_file_pointer(self, content_type, inp):
        """
        Helper function to write a file pointer to the user.
        Does not do error handling.
        """
        do_gzip = 'gzip' in self.headers.get(HTTP_HEADER_ACCEPT_ENCODING, '')

        self.send_response(HTTP_OK)
        self.send_header(HTTP_HEADER_CONTENT_TYPE, content_type)

        self.set_cache_header()
        self.set_session_cookie_header()

        if do_gzip:
            gzip_data = gzip.compress(inp.read())

            self.send_header(HTTP_HEADER_CONTENT_ENCODING, "gzip")
            self.send_header(HTTP_HEADER_VARY, HTTP_HEADER_ACCEPT_ENCODING)
            self.send_header(HTTP_HEADER_CONTENT_LENGTH, str(len(gzip_data)))

        else:
            fst = os.fstat(inp.fileno())
            self.send_header(HTTP_HEADER_CONTENT_LENGTH, str(fst[6]))

        self.end_headers()

        if self.command == 'HEAD':
            return

        elif do_gzip:
            self.wfile.write(gzip_data)

        else:
            self.copyfile(inp, self.wfile)

    def set_cache_header(self):
        """ Add cache headers if not in development """
        if not self.server.development:
            # 1 year in seconds
            cache_time = 365 * 86400

            self.send_header(
                HTTP_HEADER_CACHE_CONTROL,
                "public, max-age={}".format(cache_time))
            self.send_header(
                HTTP_HEADER_EXPIRES,
                self.date_time_string(time.time()+cache_time))

    def set_session_cookie_header(self):
        """ Add the header for the session cookie """
        if self.server.sessions.enabled and self._session is not None:
            existing_sess_id = self.get_current_session_id()

            if existing_sess_id != self._session.session_id:
                self.send_header(
                    'Set-Cookie',
                    SESSION_KEY+'='+self._session.session_id)

    def get_session(self):
        """ Get the requested session object from cookie value """
        if self.server.sessions.enabled is not True:
            return None

        session_id = self.get_current_session_id()
        if session_id is not None:
            session = self.server.sessions.get(session_id)
            if session is not None:
                session.reset_expiry()
            return session

        return None

    def get_current_session_id(self):
        """
            Extracts the current session id from the
            cookie or returns None if not set
        """
        cookie = cookies.SimpleCookie()

        if self.headers.get('Cookie', None) is not None:
            cookie.load(self.headers.get("Cookie"))

        if cookie.get(SESSION_KEY, False):
            return cookie[SESSION_KEY].value

        return None


class ServerSession:
    """ A very simple session class """
    def __init__(self, session_id):
        """ Set up the expiry time on creation """
        self._expiry = 0
        self.reset_expiry()
        self.cookie_values = {}
        self.session_id = session_id

    def reset_expiry(self):
        """ Resets the expiry based on current time """
        self._expiry = date_util.utcnow() + timedelta(
            seconds=SESSION_TIMEOUT_SECONDS)

    @property
    def is_expired(self):
        """ Return true if the session is expired based on the expiry time """
        return self._expiry < date_util.utcnow()


class SessionStore:
    """ Responsible for storing and retrieving http sessions """
    def __init__(self, enabled=True):
        """ Set up the session store """
        self._sessions = {}
        self.enabled = enabled
        self.session_lock = threading.RLock()

    @Throttle(MIN_SEC_SESSION_CLEARING)
    def remove_expired(self):
        """ Remove any expired sessions. """
        if self.session_lock.acquire(False):
            try:
                keys = []
                for key in self._sessions.keys():
                    keys.append(key)

                for key in keys:
                    if self._sessions[key].is_expired:
                        del self._sessions[key]
                        _LOGGER.info("Cleared expired session %s", key)
            finally:
                self.session_lock.release()

    def add(self, key, session):
        """ Add a new session to the list of tracked sessions """
        self.remove_expired()
        with self.session_lock:
            self._sessions[key] = session

    def get(self, key):
        """ get a session by key """
        self.remove_expired()
        session = self._sessions.get(key, None)
        if session is not None and session.is_expired:
            return None
        return session

    def create(self, api_password):
        """ Creates a new session and adds it to the sessions """
        if self.enabled is not True:
            return None

        chars = string.ascii_letters + string.digits
        session_id = ''.join([random.choice(chars) for i in range(20)])
        session = ServerSession(session_id)
        session.cookie_values[CONF_API_PASSWORD] = api_password
        self.add(session_id, session)
        return session
