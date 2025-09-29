import json
import uuid
import requests
import threading
import time
from typing import Dict, Any, Optional
from urllib.parse import urljoin


class Client:
    """
    MCP (Model Context Protocol) Client implementation.

    Example usage:
        # Create client with custom name, server host, and SSE path
        client = Client("MyApp", "http://localhost:3000", "/sse")

        # Or use default SSE path "/sse"
        client = Client("MyApp", "http://localhost:3000")

        # Initialize connection to MCP server via SSE
        if client.initialize():
            print(f"[{self.client_name}]: Connected to MCP server successfully!")
            # Use the client for MCP operations
            client.disconnect()  # Clean up when done
        else:
            print(f"[{self.client_name}]: Failed to connect to MCP server")
    """

    def __init__(
        self,
        client_name: str = "MCPClient",
        host: str = "http://localhost:3000",
        sse_path: str = "/sse",
    ):
        self.protocol_version = "2024-11-05"
        self.client_name = client_name
        self.client_version = "1.0.0"
        self.host = host.rstrip("/")  # Remove trailing slash if present
        self.sse_path = sse_path  # SSE endpoint path
        self.initialized = False
        self.server_capabilities = {}
        self.server_info = {}

        # SSE related attributes
        self.sse_endpoint = None  # Endpoint for sending messages
        self.sse_connection = None
        self.sse_thread = None
        self.message_queue = {}  # Store pending responses by request ID
        self.connection_established = False

    def initialize(self) -> bool:
        """
        Initialize MCP client using SSE transport according to MCP specification.

        Returns:
            bool: True if initialization was successful
        """
        try:
            # Step 1: Establish SSE connection
            if not self._establish_sse_connection():
                return False

            # Step 2: Wait for endpoint event
            if not self._wait_for_endpoint():
                return False

            # Step 3: Send initialize request (response will be handled by SSE listener)
            if not self._send_initialize_request():
                return False

            # Wait for initialization to complete (handled by SSE listener)
            timeout = 10  # 10 seconds timeout
            start_time = time.time()
            while not self.initialized and (time.time() - start_time) < timeout:
                time.sleep(0.1)

            if not self.initialized:
                print(
                    f"[{self.client_name}]: Timeout waiting for initialization to complete"
                )
                return False

            return True

        except Exception as e:
            print(f"[{self.client_name}]: Unexpected error during initialization: {e}")
            return False

    def _establish_sse_connection(self) -> bool:
        """
        Establish SSE connection to the server.

        Returns:
            bool: True if connection established successfully
        """
        try:
            # Start SSE connection in a separate thread
            self.sse_thread = threading.Thread(target=self._sse_listener, daemon=True)
            self.sse_thread.start()

            # Wait for connection to be established
            timeout = 10  # 10 seconds timeout
            start_time = time.time()
            while (
                not self.connection_established and (time.time() - start_time) < timeout
            ):
                time.sleep(0.1)

            if not self.connection_established:
                print(
                    f"[{self.client_name}]: Failed to establish SSE connection within timeout"
                )
                return False

            print(f"[{self.client_name}]: SSE connection established")
            return True

        except Exception as e:
            print(f"[{self.client_name}]: Failed to establish SSE connection: {e}")
            return False

    def _sse_listener(self):
        """
        Listen for SSE events from the server.
        """
        try:
            response = requests.get(
                f"{self.host}{self.sse_path}",
                headers={"Accept": "text/event-stream"},
                stream=True,
                timeout=None,
            )
            response.raise_for_status()

            # Store the connection for later cleanup
            self.sse_connection = response

            self.connection_established = True

            for line in response.iter_lines(decode_unicode=True):
                # Skip empty lines
                if not line:
                    continue

                print(f"[{self.client_name}]: Received line: {line}")

                if line.startswith("event: "):
                    event_type = line[7:]  # Remove "event: " prefix
                elif line.startswith("data: "):
                    data = line[6:]  # Remove "data: " prefix

                    if event_type == "endpoint":
                        self._handle_endpoint_event(data)
                    elif event_type == "message":
                        self._handle_message_event(data)

        except Exception as e:
            print(f"[{self.client_name}]: SSE listener error: {e}")
            self.connection_established = False

    def _handle_endpoint_event(self, data: str):
        """
        Handle endpoint event from SSE stream.

        Args:
            data: The endpoint data from the SSE event
        """
        # Server sent the endpoint for sending messages
        self.sse_endpoint = data.strip()
        print(f"[{self.client_name}]: Received endpoint: {self.sse_endpoint}")

    def _handle_message_event(self, data: str):
        """
        Handle message event from SSE stream.

        Args:
            data: The message data from the SSE event
        """
        # Server sent a message response
        try:
            message = json.loads(data)
            request_id = message.get("id")

            # Check if this is an initialize response
            if (
                request_id
                and hasattr(self, "_current_initialize_id")
                and request_id == self._current_initialize_id
            ):
                # Handle initialize response
                if self.handle_initialize_response(message):
                    # Send initialized notification
                    self._send_initialized_notification()
                    self.initialized = True
                    print(
                        f"[{self.client_name}]: MCP client initialized successfully via SSE"
                    )
                else:
                    print(f"[{self.client_name}]: Failed to handle initialize response")

            # Store in message queue for other requests
            if request_id in self.message_queue:
                self.message_queue[request_id] = message

        except json.JSONDecodeError as e:
            print(f"[{self.client_name}]: Failed to parse SSE message: {e}")

    def _wait_for_endpoint(self) -> bool:
        """
        Wait for the server to send the endpoint event.

        Returns:
            bool: True if endpoint received successfully
        """
        timeout = 10  # 10 seconds timeout
        start_time = time.time()

        while self.sse_endpoint is None and (time.time() - start_time) < timeout:
            time.sleep(0.1)

        if self.sse_endpoint is None:
            print(f"[{self.client_name}]: Timeout waiting for endpoint from server")
            return False

        return True

    def _send_initialize_request(self) -> bool:
        """
        Send initialize request to the server.

        Returns:
            bool: True if request sent and response received successfully
        """
        # Generate unique request ID
        request_id = str(uuid.uuid4())

        # Client capabilities as per MCP specification
        client_capabilities = {"roots": {"listChanged": True}, "sampling": {}}

        # Client info
        client_info = {"name": self.client_name, "version": self.client_version}

        # Create initialize request according to MCP specification
        initialize_request = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "initialize",
            "params": {
                "protocolVersion": self.protocol_version,
                "capabilities": client_capabilities,
                "clientInfo": client_info,
            },
        }

        try:
            # Store the request ID for SSE listener to handle
            self._current_initialize_id = request_id

            # Send request to the endpoint provided by server
            response = requests.post(
                self.sse_endpoint,
                json=initialize_request,
                headers={"Content-Type": "application/json"},
                timeout=30,
            )
            response.raise_for_status()

            # print("Initialize request sent successfully")
            return True

        except requests.exceptions.RequestException as e:
            print(f"[{self.client_name}]: Failed to send initialize request: {e}")
            return False

    def _send_initialized_notification(self) -> bool:
        """
        Send initialized notification to the server.

        Returns:
            bool: True if notification sent successfully
        """
        initialized_notification = {
            "jsonrpc": "2.0",
            "method": "notifications/initialized",
        }

        try:
            response = requests.post(
                self.sse_endpoint,
                json=initialized_notification,
                headers={"Content-Type": "application/json"},
                timeout=30,
            )
            response.raise_for_status()
            return True

        except requests.exceptions.RequestException as e:
            print(f"[{self.client_name}]: Failed to send initialized notification: {e}")
            return False

    def handle_initialize_response(self, response: Dict[str, Any]) -> bool:
        """
        Handle the server's response to the initialize request.

        Args:
            response: Server's initialize response

        Returns:
            bool: True if initialization was successful
        """
        if "error" in response:
            print(f"[{self.client_name}]: Initialize error: {response['error']}")
            return False

        if "result" not in response:
            print(f"[{self.client_name}]: Invalid initialize response: missing result")
            return False

        result = response["result"]

        # Validate protocol version compatibility
        server_protocol_version = result.get("protocolVersion")
        if server_protocol_version != self.protocol_version:
            print(
                f"[{self.client_name}]: Protocol version mismatch: client={self.protocol_version}, server={server_protocol_version}"
            )
            return False

        # Store server capabilities and info
        self.server_capabilities = result.get("capabilities", {})
        self.server_info = result.get("serverInfo", {})

        print(f"[{self.client_name}]: Server capabilities: {self.server_capabilities}")
        print(f"[{self.client_name}]: Server info: {self.server_info}")

        return True

    def connect(self):
        """
        Legacy method - use initialize() instead for SSE transport.
        """
        return self.initialize()

    def disconnect(self):
        """
        Disconnect from the MCP server and clean up resources.
        """
        try:
            if self.sse_connection:
                self.sse_connection.close()
            if self.sse_thread and self.sse_thread.is_alive():
                # Note: SSE thread will stop when connection is closed
                pass
            self.connection_established = False
            self.initialized = False
            print(f"[{self.client_name}]: Disconnected from MCP server")
        except Exception as e:
            print(f"[{self.client_name}]: Error during disconnect: {e}")

    def send(self, message):
        pass


if __name__ == "__main__":
    # Example usage with SSE transport
    # You can customize the SSE path based on your server configuration
    client = Client(
        client_name="MyApp",
        host="http://127.0.0.1:8181",
        sse_path="/sse",  # Customize this path as needed
    )
    if client.initialize():
        # print("MCP client initialized successfully!")
        # Perform MCP operations here
        client.disconnect()
    else:
        print(f"Failed to initialize MCP client")
