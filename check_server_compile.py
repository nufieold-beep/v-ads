import paramiko
c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect('173.208.137.202', username='root', password='Dewa@123')
stdin, stdout, stderr = c.exec_command('python3 -m py_compile /root/v-ads/liteads/ad_server/services/event_service.py')
print('OUT:', stdout.read().decode('utf-8'))
print('ERR:', stderr.read().decode('utf-8'))