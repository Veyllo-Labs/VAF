/**
 * {{APP_NAME}} - Java Application
 * 
 * {{APP_DESCRIPTION}}
 */
public class Main {
    
    /**
     * Main entry point for the application.
     * 
     * @param args Command line arguments
     */
    public static void main(String[] args) {
        System.out.println("Starting {{APP_NAME}}...");
        
        // Parse command line arguments
        if (args.length > 0) {
            System.out.println("Arguments:");
            for (int i = 0; i < args.length; i++) {
                System.out.println("  [" + i + "] " + args[i]);
            }
        }
        
        // Initialize application
        Application app = new Application();
        
        try {
            // Run the application
            app.run();
            
            System.out.println("{{APP_NAME}} completed successfully.");
        } catch (Exception e) {
            System.err.println("Error: " + e.getMessage());
            e.printStackTrace();
            System.exit(1);
        }
    }
}

/**
 * Main application class.
 */
class Application {
    
    /**
     * Run the application logic.
     * 
     * @throws Exception if an error occurs
     */
    public void run() throws Exception {
        System.out.println("Running application logic...");

        // REPLACE the call below with the real logic for your task. This working example
        // computes and prints a value so the scaffold compiles and runs out of the box.
        int result = compute(5);
        System.out.println("compute(5) = " + result);
    }

    /**
     * Example computation - REPLACE with your task's real logic.
     * Returns the sum of 1..n.
     */
    private int compute(int n) {
        int sum = 0;
        for (int i = 1; i <= n; i++) {
            sum += i;
        }
        return sum;
    }
}

