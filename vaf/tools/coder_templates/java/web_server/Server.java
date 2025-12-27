import java.io.*;
import java.net.*;
import java.util.concurrent.*;

/**
 * {{SERVER_NAME}} - Simple HTTP Server
 * 
 * {{SERVER_DESCRIPTION}}
 */
public class Server {
    
    private static final int PORT = {{PORT}};
    private static final int THREAD_POOL_SIZE = 10;
    private ServerSocket serverSocket;
    private ExecutorService threadPool;
    private boolean running = false;
    
    /**
     * Main entry point.
     * 
     * @param args Command line arguments
     */
    public static void main(String[] args) {
        Server server = new Server();
        
        try {
            server.start();
            
            // Keep server running
            System.out.println("Server running. Press Ctrl+C to stop.");
            while (server.isRunning()) {
                Thread.sleep(1000);
            }
        } catch (Exception e) {
            System.err.println("Server error: " + e.getMessage());
            e.printStackTrace();
        } finally {
            server.stop();
        }
    }
    
    /**
     * Start the server.
     * 
     * @throws IOException if server cannot start
     */
    public void start() throws IOException {
        serverSocket = new ServerSocket(PORT);
        threadPool = Executors.newFixedThreadPool(THREAD_POOL_SIZE);
        running = true;
        
        System.out.println("{{SERVER_NAME}} started on port " + PORT);
        System.out.println("Access at: http://localhost:" + PORT);
        
        // Accept connections
        while (running) {
            try {
                Socket clientSocket = serverSocket.accept();
                threadPool.submit(new ClientHandler(clientSocket));
            } catch (SocketException e) {
                if (running) {
                    System.err.println("Error accepting connection: " + e.getMessage());
                }
            }
        }
    }
    
    /**
     * Stop the server.
     */
    public void stop() {
        running = false;
        
        if (threadPool != null) {
            threadPool.shutdown();
            try {
                if (!threadPool.awaitTermination(5, TimeUnit.SECONDS)) {
                    threadPool.shutdownNow();
                }
            } catch (InterruptedException e) {
                threadPool.shutdownNow();
            }
        }
        
        if (serverSocket != null && !serverSocket.isClosed()) {
            try {
                serverSocket.close();
            } catch (IOException e) {
                System.err.println("Error closing server: " + e.getMessage());
            }
        }
        
        System.out.println("Server stopped.");
    }
    
    /**
     * Check if server is running.
     * 
     * @return true if running
     */
    public boolean isRunning() {
        return running;
    }
    
    /**
     * Client handler for processing requests.
     */
    private static class ClientHandler implements Runnable {
        private Socket clientSocket;
        
        public ClientHandler(Socket socket) {
            this.clientSocket = socket;
        }
        
        @Override
        public void run() {
            try {
                BufferedReader in = new BufferedReader(
                    new InputStreamReader(clientSocket.getInputStream())
                );
                PrintWriter out = new PrintWriter(
                    clientSocket.getOutputStream(), true
                );
                
                // Read request
                String requestLine = in.readLine();
                if (requestLine == null) {
                    return;
                }
                
                System.out.println("Request: " + requestLine);
                
                // Parse request
                String[] requestParts = requestLine.split(" ");
                String method = requestParts[0];
                String path = requestParts.length > 1 ? requestParts[1] : "/";
                
                // Handle request
                String response = handleRequest(method, path);
                
                // Send response
                out.println("HTTP/1.1 200 OK");
                out.println("Content-Type: application/json");
                out.println("Content-Length: " + response.length());
                out.println();
                out.println(response);
                
            } catch (IOException e) {
                System.err.println("Error handling client: " + e.getMessage());
            } finally {
                try {
                    clientSocket.close();
                } catch (IOException e) {
                    System.err.println("Error closing client socket: " + e.getMessage());
                }
            }
        }
        
        /**
         * Handle HTTP request.
         * 
         * @param method HTTP method
         * @param path Request path
         * @return Response body
         */
        private String handleRequest(String method, String path) {
            // TODO: Implement request handling logic
            
            if (path.equals("/")) {
                return "{\"message\": \"Welcome to {{SERVER_NAME}}\", \"status\": \"running\"}";
            } else if (path.equals("/api/health")) {
                return "{\"status\": \"healthy\", \"service\": \"{{SERVER_NAME}}\"}";
            } else {
                return "{\"error\": \"Not found\", \"path\": \"" + path + "\"}";
            }
        }
    }
}

