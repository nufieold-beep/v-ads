import time, paramiko
c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect('173.208.137.202', username='root', password='Dewa@123')
stdin, stdout, stderr = c.exec_command('docker ps --filter name=v-ads-ad-server')
print('PS:', stdout.read().decode('utf-8'))
stdin, stdout, stderr = c.exec_command('docker logs --tail 20 v-ads-ad-server-1')
print('LOGS:', stderr.read().decode('utf-8'))