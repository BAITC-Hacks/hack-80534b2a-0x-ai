"""Interactive local startup, including first-run account setup and API key."""
import getpass
import os
import socket
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import auth
import main as app
import uvicorn


def main():
    with socket.socket() as sock:
        if sock.connect_ex(('127.0.0.1', 8000)) == 0:
            print('Port 8000 is already in use. Stop the current server before running this command.')
            return 1
    app.init_state()
    with app.connect() as conn:
        configured = conn.execute('SELECT count(*) FROM accounts').fetchone()[0]
    if not configured:
        employee_id = os.getenv('DEMO_EMPLOYEE_ID', 'E0001')
        if employee_id not in app.STATE['employees']:
            print('DEMO_EMPLOYEE_ID must identify an existing employee.')
            return 1
        accounts = [('employee', 'employee', employee_id), ('hr', 'hr', None)]
        pending = []
        print('First startup: create separate passwords for employee and HR accounts (12+ characters).')
        for username, role, eid in accounts:
            while True:
                password = getpass.getpass(f'Password for {username} (hidden): ')
                if not 12 <= len(password) <= 256:
                    print('Use 12 to 256 characters.')
                    continue
                if password != getpass.getpass('Repeat password: '):
                    print('Passwords do not match.')
                    continue
                pending.append((username, auth.password_hash(password), role, eid))
                break
        with app.connect() as conn:
            conn.executemany('INSERT INTO accounts(username,password_hash,role,employee_id) VALUES(?,?,?,?)', pending)
        print('Accounts created: employee and hr. Passwords are stored only as salted hashes.')
    print('AI provider:', 'OpenAI' if os.getenv('OPENAI_API_KEY') else 'rules fallback')
    print('Configure the OpenAI key on the website after signing in as HR.')
    uvicorn.run(app.app, host='127.0.0.1', port=8000)
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (KeyboardInterrupt, EOFError):
        print('\nStartup cancelled.')
