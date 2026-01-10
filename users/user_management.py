#!/usr/bin/env python3
"""
User Management CLI Script for Dibba

This script provides a command-line interface for managing users in Dibba.
It allows you to create, list, update passwords, and delete users.

Usage:
    python scripts/user_management.py list
    python scripts/user_management.py create <username> <password>
    python scripts/user_management.py update-password <username> <new_password>
    python scripts/user_management.py delete <username>
"""
import sys
import argparse
import getpass
from pathlib import Path

# Add parent directory to path to import dibba modules
sys.path.insert(0, str(Path(__file__).parent.parent))

from utils.redis.redis_interface import RedisInterface
from utils.extensions.utilities_extention import UtilitiesExtension
from utils.ReadConfig import ReadConfig
from logpkg.log_kcld import LogKCld

logger = LogKCld()


def get_redis_and_encoder():
    """Initialize Redis interface and encoder."""
    read_config = ReadConfig()
    key = read_config.encryption_config['key']
    encoder = UtilitiesExtension(key)
    redis_interface = RedisInterface(
        read_config.redis_db_config['redis_host'],
        read_config.redis_db_config['redis_port'],
        read_config.redis_db_config['redis_db']
    )
    return redis_interface, encoder


def list_users():
    """List all users."""
    try:
        rd, _ = get_redis_and_encoder()
        all_users = rd.redis_client.hgetall("authentication")
        
        if not all_users:
            print("No users found.")
            return
        
        print(f"\nFound {len(all_users)} user(s):\n")
        print(f"{'Username':<30}")
        print("-" * 30)
        for username in sorted(all_users.keys()):
            print(f"{username:<30}")
        print()
    except Exception as e:
        print(f"Error listing users: {e}", file=sys.stderr)
        sys.exit(1)


def create_user(username: str, password: str = None):
    """Create a new user."""
    try:
        rd, encoder = get_redis_and_encoder()
        
        # Check if user already exists
        if rd.get_user_pass(username):
            print(f"Error: User '{username}' already exists.", file=sys.stderr)
            sys.exit(1)
        
        # Get password if not provided
        if not password:
            password = getpass.getpass(f"Enter password for user '{username}': ")
            password_confirm = getpass.getpass("Confirm password: ")
            if password != password_confirm:
                print("Error: Passwords do not match.", file=sys.stderr)
                sys.exit(1)
        
        # Hash password and save
        hashed_password = encoder.encode_phrase_with_key(password)
        rd.save_user_pass(username, hashed_password)
        
        print(f"User '{username}' created successfully.")
    except Exception as e:
        print(f"Error creating user: {e}", file=sys.stderr)
        sys.exit(1)


def update_password(username: str, new_password: str = None):
    """Update user password."""
    try:
        rd, encoder = get_redis_and_encoder()
        
        # Check if user exists
        if not rd.get_user_pass(username):
            print(f"Error: User '{username}' not found.", file=sys.stderr)
            sys.exit(1)
        
        # Get new password if not provided
        if not new_password:
            new_password = getpass.getpass(f"Enter new password for user '{username}': ")
            password_confirm = getpass.getpass("Confirm new password: ")
            if new_password != password_confirm:
                print("Error: Passwords do not match.", file=sys.stderr)
                sys.exit(1)
        
        # Hash new password and save
        hashed_password = encoder.encode_phrase_with_key(new_password)
        rd.save_user_pass(username, hashed_password)
        
        print(f"Password updated for user '{username}' successfully.")
    except Exception as e:
        print(f"Error updating password: {e}", file=sys.stderr)
        sys.exit(1)


def delete_user(username: str, force: bool = False):
    """Delete a user."""
    try:
        rd, _ = get_redis_and_encoder()
        
        # Check if user exists
        if not rd.get_user_pass(username):
            print(f"Error: User '{username}' not found.", file=sys.stderr)
            sys.exit(1)
        
        # Confirm deletion unless forced
        if not force:
            confirm = input(f"Are you sure you want to delete user '{username}'? (yes/no): ")
            if confirm.lower() not in ['yes', 'y']:
                print("Deletion cancelled.")
                return
        
        # Delete user from Redis
        rd.redis_client.hdel("authentication", username)
        
        print(f"User '{username}' deleted successfully.")
    except Exception as e:
        print(f"Error deleting user: {e}", file=sys.stderr)
        sys.exit(1)


def main():
    """Main CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Dibba User Management CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s list
  %(prog)s create admin mypassword123
  %(prog)s create user1  # Will prompt for password
  %(prog)s update-password admin newpassword123
  %(prog)s update-password admin  # Will prompt for password
  %(prog)s delete user1
  %(prog)s delete user1 --force  # Skip confirmation
        """
    )
    
    subparsers = parser.add_subparsers(dest='command', help='Command to execute')
    
    # List command
    list_parser = subparsers.add_parser('list', help='List all users')
    
    # Create command
    create_parser = subparsers.add_parser('create', help='Create a new user')
    create_parser.add_argument('username', help='Username for the new user')
    create_parser.add_argument('password', nargs='?', help='Password (optional, will prompt if not provided)')
    
    # Update password command
    update_parser = subparsers.add_parser('update-password', help='Update user password')
    update_parser.add_argument('username', help='Username to update')
    update_parser.add_argument('new_password', nargs='?', help='New password (optional, will prompt if not provided)')
    
    # Delete command
    delete_parser = subparsers.add_parser('delete', help='Delete a user')
    delete_parser.add_argument('username', help='Username to delete')
    delete_parser.add_argument('--force', action='store_true', help='Skip confirmation prompt')
    
    args = parser.parse_args()
    
    if not args.command:
        parser.print_help()
        sys.exit(1)
    
    # Execute command
    if args.command == 'list':
        list_users()
    elif args.command == 'create':
        create_user(args.username, args.password)
    elif args.command == 'update-password':
        update_password(args.username, args.new_password)
    elif args.command == 'delete':
        delete_user(args.username, args.force)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == '__main__':
    main()

