"""Capture the actual ORTB payload being sent to the DSP from docker logs."""
import paramiko, json

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect('173.208.137.202', username='root', password='Dewa@123')

# Get the last few ORTB bid request log entries
stdin, stdout, stderr = ssh.exec_command(
    'docker logs liteads-ad-server-1 --tail 200 2>&1 | grep "Sending ORTB bid request" | tail -3'
)
lines = stdout.read().decode().strip().split('\n')
print(f"Found {len(lines)} ORTB request log entries\n")

for i, line in enumerate(lines):
    try:
        data = json.loads(line)
        # Print the interesting fields
        keys_to_show = [
            'ortb_bundle', 'ortb_app_name', 'ortb_ip', 'ortb_ifa',
            'ortb_devicetype', 'ortb_os', 'ortb_make', 'ortb_country',
            'has_source', 'has_schain', 'source_tid',
            'has_user_id', 'has_user_eids', 'has_regs',
        ]
        print(f"--- Entry {i+1} ---")
        for k in keys_to_show:
            if k in data:
                print(f"  {k}: {data[k]}")
        print()
    except json.JSONDecodeError:
        print(f"  (not JSON): {line[:200]}")

# Now get a recent DSP response log to see bid status
stdin, stdout, stderr = ssh.exec_command(
    'docker logs liteads-ad-server-1 --tail 200 2>&1 | grep -E "DSP (response|returned|no.bid|bid)" | tail -5'
)
resp_lines = stdout.read().decode().strip()
print(f"\nDSP Response logs:\n{resp_lines[:1000]}")

ssh.close()
