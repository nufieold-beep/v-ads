"""Send a test ORTB request and capture the exact payload via debug logging."""
import paramiko, json, time

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect('173.208.137.202', username='root', password='Dewa@123')

# Trigger a test request
stdin, stdout, stderr = ssh.exec_command(
    'curl -s "http://localhost:8000/api/vast?sid=ctv_preroll&w=1920&h=1080'
    '&ip=70.160.142.19'
    '&ua=Mozilla%2F5.0+%28Linux%3B+Android+9%3B+AFTTIFF43+Build%2FPS7681.5384N%3B+wv%29PlexTV%2F10.21.1.1562'
    '&app_bundle=B079DRM7ZC'
    '&app_name=Wdam+7+News'
    '&app_store_url=https%3A%2F%2Fwww.amazon.com%2Fdp%2Fb079drm7zc'
    '&country_code=US'
    '&max_dur=30&min_dur=5'
    '&device_make=AmazonFireStick'
    '&device_type=3'
    '&ct_genre=Entertainment%2Creality-tv%2Cdrama%2Caction%2Ccomedy%2Cdocumentary'
    '&ct_rating=TV-G'
    '&ct_livestream=1'
    '&ct_len=1800'
    '&ct_lang=en'
    '&dnt=0'
    '&ifa=932cb552-561b-46aa-bb02-5a0e9d562652'
    '&ifa_type=afai'
    '&os=Android'
    '&bidfloor=4'
    '&us_privacy=1YNN" > /dev/null 2>&1'
)
stdout.read()
time.sleep(2)

# Now get the last ORTB log with the full payload details
stdin, stdout, stderr = ssh.exec_command(
    'docker logs liteads-ad-server-1 --tail 50 2>&1 | grep "Sending ORTB bid request" | tail -1'
)
line = stdout.read().decode().strip()
if line:
    try:
        data = json.loads(line)
        print("=== LAST ORTB BID REQUEST LOG ===")
        for k, v in sorted(data.items()):
            if k not in ('logger_name', 'level', 'timestamp'):
                print(f"  {k}: {v}")
    except json.JSONDecodeError:
        print(f"Raw: {line[:500]}")
else:
    print("No ORTB request log found")

# Also check for any errors
stdin, stdout, stderr = ssh.exec_command(
    'docker logs liteads-ad-server-1 --tail 50 2>&1 | grep -iE "error|traceback|exception" | tail -5'
)
errs = stdout.read().decode().strip()
if errs:
    print(f"\nERRORS:\n{errs}")
else:
    print("\nNo errors found")

# Check DSP responses
stdin, stdout, stderr = ssh.exec_command(
    'docker logs liteads-ad-server-1 --tail 50 2>&1 | grep -iE "DSP|no.bid|204|bid.response" | tail -5'
)
resp = stdout.read().decode().strip()
if resp:
    print(f"\nDSP activity:\n{resp[:500]}")

ssh.close()
