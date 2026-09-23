"""Local administrator provisioning; there is no public registration endpoint."""
import argparse
import getpass
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import auth
import main as app


def main():
    parser = argparse.ArgumentParser(description='Create a local Career Quest login.')
    parser.add_argument('--username', required=True)
    parser.add_argument('--role', choices=['employee', 'hr'], required=True)
    parser.add_argument('--employee-id')
    args = parser.parse_args()
    app.init_state()
    if args.role == 'employee' and args.employee_id not in app.STATE['employees']:
        parser.error('Employee ID must exist in the dataset or imported profiles.')
    with app.connect() as conn:
        if conn.execute('SELECT 1 FROM accounts WHERE username=?', (args.username.strip().lower(),)).fetchone():
            parser.error('This username already exists. No password was changed.')
    password = getpass.getpass('Password (12+ characters, hidden): ')
    if password != getpass.getpass('Repeat password: '):
        parser.error('Passwords do not match.')
    try:
        auth.create_account(args.username, password, args.role, args.employee_id)
    except (ValueError, sqlite3.IntegrityError) as exc:
        parser.error(str(exc))
    print('Account created. Sign in at http://127.0.0.1:8000/')


if __name__ == '__main__':
    main()
