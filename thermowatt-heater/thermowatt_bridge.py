import sys, json, time, uuid, os, requests, urllib3
import paho.mqtt.client as mqtt
from paho.mqtt.enums import CallbackAPIVersion

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# --- CONFIGURATION ---
EMAIL = sys.argv[1] if len(sys.argv) > 1 else None
PASSWORD = sys.argv[2] if len(sys.argv) > 2 else None
CONFIG_FILE = "/data/thermowatt_config.json" if os.path.exists("/data") else "thermowatt_config.json"
MQTT_HOST = os.getenv("MQTT_HOST", "core-mosquitto")
MQTT_PORT = int(os.getenv("MQTT_PORT", 1883))
MQTT_USER = os.getenv("MQTT_USER")
MQTT_PASS = os.getenv("MQTT_PASSWORD")

class MyThermowattBridge:
    API_KEY = "YVjArWssxKH631jv1dnnWOTr6gijsSAGz7rQJ4hJoUNRffxYvbQaMbePBEZalena"
    BASE_URL = "https://myapp-connectivity.com/api/v1"

    def __init__(self):
        self.config = self._load_config()
        self.session = requests.Session()
        self.session.headers.update({
            "app": "MyThermowatt", "platform": "iOS", "version": "3.14",
            "x-api-key": self.API_KEY, "lang": "en"
        })
        self.mqtt_client = mqtt.Client(CallbackAPIVersion.VERSION2)
        if MQTT_USER: self.mqtt_client.username_pw_set(MQTT_USER, MQTT_PASS)

    def _load_config(self):
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, 'r') as f: return json.load(f)
        return {"device_uuid": str(uuid.uuid4()), "access_token": None, "refresh_token": None}

    def _save_config(self):
        with open(CONFIG_FILE, 'w') as f: json.dump(self.config, f)

    def _update_auth(self, access, refresh):
        self.config.update({"access_token": access, "refresh_token": refresh})
        self.session.headers.update({"Authorization": f"Bearer {access}"})
        self._save_config()

    def login(self):
        payload = {"username": EMAIL, "password": PASSWORD, "device_id": self.config["device_uuid"]}
        r = self.session.post(f"{self.BASE_URL}/login", json=payload, verify=False)
        r.raise_for_status()
        res = r.json()['result']
        self._update_auth(res['accessToken'], res['refreshToken'])

    def refresh_session(self):
        payload = {"username": EMAIL, "refreshToken": self.config["refresh_token"]}
        r = self.session.post(f"{self.BASE_URL}/refresh", json=payload, verify=False)
        if r.status_code == 200:
            res = r.json()['result']
            self._update_auth(res['accessToken'], res['refreshToken'])
            return True
        return False

    def request(self, method, endpoint, **kwargs):
        url = f"{self.BASE_URL}{endpoint}"
        resp = self.session.request(method, url, verify=False, **kwargs)
        if resp.status_code == 401 and self.refresh_session():
            return self.session.request(method, url, verify=False, **kwargs)
        return resp

    # --- MQTT HANDLING ---
    def publish_discovery(self):
        sn = self.config['serial']
        topic = f"homeassistant/water_heater/{sn}/config"
        payload = {
            "unique_id": f"thermowatt_{sn}_v314",
            "name": f"Boiler {self.config['name']}",
            "temp_unit": "C", 
            "min_temp": 20, 
            "max_temp": 75,
            "optimistic": True,  
            "current_temperature_topic": f"P/{sn}/STATUS",
            "current_temperature_template": "{{ value_json.result.T_Avg | default(0) | float }}",
            "temperature_state_topic": f"P/{sn}/STATUS",
            "temperature_state_template": "{{ value_json.result.T_SetPoint | default(0) | float }}",
            "temperature_command_topic": f"P/{sn}/CMD/TEMP",
            "mode_state_topic": f"P/{sn}/STATUS",
            "mode_state_template": (
                "{% set cmd = value_json.result.Cmd | default(0) | int %}"
                "{% if cmd == 9 %}Manual"
                "{% elif cmd == 3 %}Eco"
                "{% elif cmd == 17 %}Auto"
                "{% elif cmd == 65 %}Holiday"
                "{% elif cmd == 16 %}off"
                "{% else %}Off{% endif %}" 
            ),
            "mode_command_topic": f"P/{sn}/CMD/MODE",
            "modes": ["Off", "Eco", "Manual", "Auto", "Holiday"],
            "device": {"identifiers": [f"tw_{sn}"], "manufacturer": "Thermowatt", "name": self.config['name']}
        }
        self.mqtt_client.publish(topic, json.dumps(payload), retain=True)

    def on_mqtt_message(self, client, userdata, msg):
        try:
            sn = self.config['serial']
            payload = msg.payload.decode()
            # Stop the polling loop from overwriting our change for 10 seconds
            self.last_command_time = time.time() 
            
            # Use the "favorite" temp if it exists, otherwise default 60
            current_fav = self.config.get("last_setpoint", 60)

            if f"P/{sn}/CMD/TEMP" in msg.topic:
                temp = int(float(payload))
                print(f"[CMD] Setting Temperature to {temp}C...")
                self.request("POST", "/manual", json={"T_SetPoint": temp})
                self.config["last_setpoint"] = temp
                # Force HA to show this temperature immediately
                self._inject_fake_status({"T_SetPoint": str(temp)})
            
            elif f"P/{sn}/CMD/MODE" in msg.topic:
                print(f"[CMD] Setting Mode to {payload}...")
                if payload == "Manual":
                    self.request("POST", "/manual", json={"T_SetPoint": current_fav})
                    self._inject_fake_status({"Cmd": "9", "T_SetPoint": str(current_fav)})
                elif payload == "Eco":
                    self.request("POST", "/eco", headers={"Content-Type": "text/plain"}, data="")
                    self._inject_fake_status({"Cmd": "3"})
                elif payload == "Auto":
                    self.request("POST", "/auto", headers={"Content-Type": "text/plain"}, data="")
                    self._inject_fake_status({"Cmd": "17"})
                elif payload == "Holiday":
                    import datetime
                    # 1. Calculate future date (1 month)
                    future_date = (datetime.datetime.now() + datetime.timedelta(days=30)).strftime("%Y-%m-%d")
                    print(f"[CMD] Setting Holiday Mode until {future_date}...")
                    # 2. Issue the API command
                    resp = self.request("POST", "/holiday", json={"end_date": future_date})
                    # 3. SET AN EXTENDED LOCKOUT (90 seconds for Holiday mode)
                    self.last_command_time = time.time() + 30 # Adds a 30s "buffer" to the standard 60s
                    # 4. Immediate state injection so HA doesn't flicker
                    self._inject_fake_status({"Cmd": "65"})
                elif payload == "Off":
                    print("[CMD] Turning Boiler OFF...")
                    resp = self.request("POST", "/off", headers={"Content-Type": "text/plain"}, data="")
                    self._inject_fake_status({"Cmd": "16"})
                    if resp:
                        print(f"[SUCCESS] Boiler is now OFF: {resp.text}")
            self._save_config()
        except Exception as e:
            print(f"MQTT Cmd Error: {e}")

    def _inject_fake_status(self, overrides):
        """Immediately updates HA state to prevent flipping while cloud syncs """
        try:
            # Get the base status to preserve T_Avg and other fields
            status = self.request("GET", "/status").json()
            for k, v in overrides.items():
                status['result'][k] = str(v) # API uses strings for values 
            self.mqtt_client.publish(f"P/{self.config['serial']}/STATUS", json.dumps(status), retain=True)
        except: pass

    def poll_and_publish(self):
        # If we sent a command in the last 10 seconds, skip polling
        # to let the backend cloud catch up.
        if hasattr(self, 'last_command_time') and (time.time() - self.last_command_time < 60):
            return 

        try:
            r = self.request("GET", "/status")
            if r.status_code == 200:
                self.mqtt_client.publish(f"P/{self.config['serial']}/STATUS", r.text, retain=True)
        except Exception as e:
            print(f"Polling Error: {e}")

    def run(self):
        print("--- BOOT SEQUENCE START ---")
        
        # 1. Credentials Check
        if not EMAIL or not PASSWORD:
            print("FAILED: Step 1 - Missing EMAIL/PASSWORD in addon config.")
            sys.exit(1)
        print("OK: Step 1 - Credentials present.")

        # 2 & 3. MQTT Check
        try:
            self.mqtt_client.connect(MQTT_HOST, MQTT_PORT, 60)
            print("OK: Step 2 & 3 - Connected and authenticated with local MQTT.")
        except Exception as e:
            print(f"FAILED: Step 2/3 - MQTT Connection Error: {e}")
            sys.exit(1)

        # 4. Backend Login
        try:
            self.login()
            print("OK: Step 4 - Logged in to Thermowatt backend.")
        except Exception as e:
            print(f"FAILED: Step 4 - Backend authentication failed: {e}")
            sys.exit(1)

        # 5. Discover Heaters
        try:
            r = self.request("GET", "/user-info")
            devices = r.json().get('result', {}).get('termostati', [])
            if not devices: raise Exception("Zero devices returned")
            dev = devices[0]
            self.config.update({"serial": dev['seriale'], "name": dev.get('nome', 'Boiler')})
            self.session.headers.update({"seriale": self.config['serial']})
            print(f"OK: Step 5 - Found {len(devices)} thermostats. Using: {self.config['name']}")
        except Exception as e:
            print(f"FAILED: Step 5 - Could not retrieve thermostat list: {e}")
            sys.exit(1)

        # 6. Initial Status
        try:
            self.poll_and_publish()
            print("OK: Step 6 - Successfully fetched initial status.")
        except Exception as e:
            print(f"FAILED: Step 6 - Initial status fetch failed: {e}")
            sys.exit(1)

        print("OK: Step 7 - Booted successfully.")
        
        # 8 & 9. Finalize and Start Loop
        self.publish_discovery()
        self.mqtt_client.on_message = self.on_mqtt_message
        self.mqtt_client.subscribe(f"P/{self.config['serial']}/CMD/#")
        self.mqtt_client.loop_start()
        
        print("OK: Step 8 - Beginning 60s polling loop.")
        while True:
            self.poll_and_publish()
            time.sleep(60)

if __name__ == "__main__":
    bridge = MyThermowattBridge()
    bridge.run()
