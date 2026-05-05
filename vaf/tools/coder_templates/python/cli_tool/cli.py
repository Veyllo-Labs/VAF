#!/usr/bin/env python3
"""
{{CLI_TOOL_NAME}} - Command Line Tool

{{CLI_DESCRIPTION}}
"""

import sys
import argparse
from typing import List, Optional


class CLITool:
    """Main CLI tool class."""
    
    def __init__(self):
        self.parser = argparse.ArgumentParser(
            prog='{{CLI_TOOL_NAME}}',
            description='{{CLI_DESCRIPTION}}',
            formatter_class=argparse.RawDescriptionHelpFormatter,
            epilog='''
Examples:
  {{CLI_TOOL_NAME}} --input file.txt --output result.txt
  {{CLI_TOOL_NAME}} --verbose --dry-run
            '''
        )
        self._setup_arguments()
    
    def _setup_arguments(self):
        """Setup command line arguments."""
        self.parser.add_argument(
            '--input',
            '-i',
            type=str,
            required=True,
            help='Input file or data source'
        )
        
        self.parser.add_argument(
            '--output',
            '-o',
            type=str,
            help='Output file path (optional)'
        )
        
        self.parser.add_argument(
            '--verbose',
            '-v',
            action='store_true',
            help='Enable verbose output'
        )
        
        self.parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Perform a dry run without making changes'
        )
        
        # Add subcommands if needed
        subparsers = self.parser.add_subparsers(
            dest='command',
            help='Available commands'
        )
        
        # Example subcommand
        process_parser = subparsers.add_parser(
            'process',
            help='Process data'
        )
        process_parser.add_argument(
            '--format',
            choices=['json', 'csv', 'txt'],
            default='json',
            help='Output format'
        )
    
    def run(self, args: Optional[List[str]] = None) -> int:
        """
        Run the CLI tool.
        
        Args:
            args: Command line arguments (defaults to sys.argv)
            
        Returns:
            Exit code (0 for success, non-zero for error)
        """
        parsed_args = self.parser.parse_args(args)
        
        if parsed_args.verbose:
            print(f"Running {{CLI_TOOL_NAME}}...")
            print(f"Arguments: {parsed_args}")
        
        if parsed_args.dry_run:
            print("DRY RUN MODE - No changes will be made")
            return 0
        
        # Handle subcommands
        if parsed_args.command == 'process':
            return self._handle_process(parsed_args)
        
        # Main logic
        try:
            result = self._execute(parsed_args)
            if parsed_args.verbose:
                print(f"Result: {result}")
            return 0
        except Exception as e:
            print(f"Error: {e}", file=sys.stderr)
            if parsed_args.verbose:
                import traceback
                traceback.print_exc()
            return 1
    
    def _execute(self, args) -> str:
        """
        Execute the main logic.
        
        Args:
            args: Parsed arguments
            
        Returns:
            Result string
        """
        # TODO: Implement your CLI logic here
        return f"Processing {args.input}..."
    
    def _handle_process(self, args) -> int:
        """
        Handle the 'process' subcommand.
        
        Args:
            args: Parsed arguments
            
        Returns:
            Exit code
        """
        # TODO: Implement process command logic
        print(f"Processing with format: {args.format}")
        return 0


def main():
    """Entry point for the CLI tool."""
    tool = CLITool()
    try:
        exit_code = tool.run()
        sys.exit(exit_code)
    except KeyboardInterrupt:
        print("\nInterrupted by user", file=sys.stderr)
        sys.exit(130)
    except Exception as e:
        print(f"Fatal error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()

