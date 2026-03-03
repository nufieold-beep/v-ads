import time
import paramiko
c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect('173.208.137.202', username='root', password='Dewa@123')
stdin, stdout, stderr = c.exec_command('docker exec -i v-ads-postgres-1 psql -U liteads -d liteads -c "select event_type, count(*) from ad_events group by event_type;"')
print('Before:', stdout.read().decode('utf-8'))
time.sleep(10)
stdin, stdout, stderr = c.exec_command('docker exec -i v-ads-postgres-1 psql -U liteads -d liteads -c "select event_type, count(*) from ad_events group by event_type;"')
print('After:', stdout.read().decode('utf-8'))