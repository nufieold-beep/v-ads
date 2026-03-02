"""Capture and pretty-print the actual ORTB payload from server logs."""
import paramiko, json

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect('173.208.137.202', username='root', password='Dewa@123')

# Get the last ORTB payload DEBUG log entry
stdin, stdout, stderr = ssh.exec_command(
    'docker logs liteads-ad-server-1 --tail 100 2>&1 | grep "ORTB payload DEBUG" | tail -1'
)
line = stdout.read().decode().strip()

if line:
    try:
        log_data = json.loads(line)
        payload_str = log_data.get('payload', '')
        payload = json.loads(payload_str)
        print("=== ACTUAL ORTB PAYLOAD BEING SENT TO DSP ===")
        print(json.dumps(payload, indent=2))
    except (json.JSONDecodeError, KeyError) as e:
        print(f"Parse error: {e}")
        print(f"Raw: {line[:2000]}")
else:
    print("No ORTB payload DEBUG log found")

ssh.close()
