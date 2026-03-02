"""Deploy modified files to the production server."""
import paramiko, tarfile, io, os, time

os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect('173.208.137.202', username='root', password='Dewa@123')

# Create tar archive with all modified files
buf = io.BytesIO()
with tarfile.open(fileobj=buf, mode='w:gz') as tar:
    tar.add('liteads/ad_server/services/demand_forwarder.py',
            arcname='liteads/ad_server/services/demand_forwarder.py')
    tar.add('liteads/schemas/openrtb.py',
            arcname='liteads/schemas/openrtb.py')
    tar.add('liteads/common/ortb_enricher.py',
            arcname='liteads/common/ortb_enricher.py')
buf.seek(0)

# Upload tar to server
sftp = ssh.open_sftp()
with sftp.file('/tmp/liteads_patch.tar.gz', 'wb') as f:
    f.write(buf.read())
sftp.close()
print('Uploaded tar archive (3 files)')

# Copy into container and extract
cmds = [
    'docker cp /tmp/liteads_patch.tar.gz liteads-ad-server-1:/app/liteads_patch.tar.gz',
    'docker exec liteads-ad-server-1 tar xzf /app/liteads_patch.tar.gz -C /app/',
    'docker exec liteads-ad-server-1 find /app -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null ; true',
    'docker restart liteads-ad-server-1',
]
for cmd in cmds:
    stdin, stdout, stderr = ssh.exec_command(cmd)
    out = stdout.read().decode().strip()
    err = stderr.read().decode().strip()
    if out:
        print(f'  OUT: {out[:200]}')
    if err and 'No such file' not in err:
        print(f'  ERR: {err[:200]}')

print('Deployed and restarted container')

# Wait for container to come up
time.sleep(8)

# Health check
stdin, stdout, stderr = ssh.exec_command('curl -s http://localhost:8000/health')
health = stdout.read().decode().strip()
print(f'Health: {health}')

# Container status
stdin, stdout, stderr = ssh.exec_command('docker ps --filter name=liteads-ad-server-1 --format "{{.Status}}"')
status = stdout.read().decode().strip()
print(f'Container: {status}')

ssh.close()
