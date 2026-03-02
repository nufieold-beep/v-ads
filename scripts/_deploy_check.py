"""Quick deploy health-check script."""
import paramiko, time

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect('173.208.137.202', username='root', password='Dewa@123')

# Check container status
stdin, stdout, stderr = ssh.exec_command('docker ps --filter name=liteads-ad-server-1 --format "{{.Status}}"')
status = stdout.read().decode().strip()
print(f'Container status: {status}')

# Wait for startup
time.sleep(5)

# Health check
stdin, stdout, stderr = ssh.exec_command('curl -s http://localhost:8000/health')
health = stdout.read().decode().strip()
print(f'Health: {health[:300]}')

# Check recent logs for errors
stdin, stdout, stderr = ssh.exec_command('docker logs liteads-ad-server-1 --tail 30 2>&1')
logs = stdout.read().decode().strip()
print(f'Recent logs:\n{logs[-1200:]}')

ssh.close()
