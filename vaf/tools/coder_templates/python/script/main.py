#!/usr/bin/env python3
"""
{{SCRIPT_NAME}} - {{SCRIPT_DESCRIPTION}}

{{SCRIPT_DETAILS}}
"""

import sys
import argparse
from typing import Optional


def main(args: Optional[list] = None) -> int:
    """
    Main function for the script.
    
    Args:
        args: Command line arguments (defaults to sys.argv)
        
    Returns:
        Exit code (0 for success, non-zero for error)
    """
    parser = argparse.ArgumentParser(
        description="{{SCRIPT_DESCRIPTION}}",
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    
    # Add your arguments here
    parser.add_argument(
        '--input',
        type=str,
        help='Input file or data'
    )
    
    parser.add_argument(
        '--output',
        type=str,
        help='Output file path'
    )
    
    parser.add_argument(
        '--verbose',
        action='store_true',
        help='Enable verbose output'
    )
    
    parsed_args = parser.parse_args(args)
    
    # Your script logic here
    if parsed_args.verbose:
        print(f"Running {{SCRIPT_NAME}}...")
        print(f"Input: {parsed_args.input}")
        print(f"Output: {parsed_args.output}")
    
    # TODO: Implement your script logic
    
    return 0


if __name__ == '__main__':
    try:
        exit_code = main()
        sys.exit(exit_code)
    except KeyboardInterrupt:
        print("\nInterrupted by user", file=sys.stderr)
        sys.exit(130)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

