import paramiko
c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect('173.208.137.202', username='root', password='Dewa@123')
stdin, stdout, stderr = c.exec_command('cd /root/v-ads && docker compose stop ad-server && docker compose rm -f ad-server && docker compose up -d --build --force-recreate ad-server')
print('OUT:', stdout.read().decode('utf-8'))
print('ERR:', stderr.read().decode('utf-8'))