import time
import pyaudio
import requests
from clapDetector import ClapDetector

# ── Configuration ─────────────────────────────────────────────────────────────

WEBHOOK_URL = "http://192.168.50.76:5678/webhook/clap"

THRESHOLD_BIAS = 6000
LOWCUT         = 200
HIGHCUT        = 3200

# ── Webhook sender ─────────────────────────────────────────────────────────────

def trigger_webhook(clap_count):
    try:
        response = requests.post(WEBHOOK_URL, json={"claps": clap_count}, timeout=3)
        print(f"Sent {clap_count} clap(s) → {WEBHOOK_URL} [{response.status_code}]")
    except requests.exceptions.RequestException as e:
        print(f"Webhook failed: {e}")

# ── Find USB microphone index ──────────────────────────────────────────────────

def find_usb_mic():
    p = pyaudio.PyAudio()
    device_index = None
    for i in range(p.get_device_count()):
        info = p.get_device_info_by_index(i)
        if info['maxInputChannels'] > 0 and 'USB' in info['name']:
            print(f"Found USB mic at index {i}: {info['name']}")
            device_index = i
            break
    p.terminate()
    return device_index

# ── Main loop ──────────────────────────────────────────────────────────────────

def main():
    print("Initialising audio...")

    detector = None
    for attempt in range(10):
        try:
            device_index = find_usb_mic()
            if device_index is None:
                raise OSError("No USB microphone found")

            detector = ClapDetector(
                inputDevice=device_index,
                logLevel=20,
                bufferLength=4096,
                exceptionOnOverflow=False,
                resetTime=0.6
            )
            detector.initAudio()
            break
        except OSError as e:
            print(f"Audio init failed (attempt {attempt + 1}/10): {e} — retrying in 5s")
            time.sleep(5)

    if detector is None:
        print("Could not initialise audio after 10 attempts. Exiting.")
        raise SystemExit(1)

    print("Listening for claps — clap near the microphone now!")

    try:
        while True:
            audioData = detector.getAudio()
            result = detector.run(
                thresholdBias=THRESHOLD_BIAS,
                lowcut=LOWCUT,
                highcut=HIGHCUT,
                audioData=audioData
            )
            count = len(result)
            if count in (1, 2, 3):
                print(f"Detected {count} clap(s)!")
                trigger_webhook(count)
                time.sleep(1)

    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        detector.stop()

if __name__ == "__main__":
    main()

