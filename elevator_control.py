"""
Smart Elevator Automation System
---------------------------------
Raspberry Pi 3 based elevator controller with:
  - PIR motion sensor    (GPIO 10)
  - HC-SR04 ultrasonic   (TRIG: GPIO 17, ECHO: GPIO 27)
  - USB Webcam           (OpenCV Haar Cascade face detection)
  - Push buttons         (3rd floor: GPIO 13, 6th floor: GPIO 19)
  - Relay: door          (GPIO 22)
  - Relay: motor up      (GPIO 23)
  - Relay: motor down    (GPIO 24)
  - Flask + Socket.IO    (real-time web dashboard on port 5000)

Sensor fusion rule: at least 2-of-3 sensors must confirm human
presence before the elevator opens its doors at a requested floor.
"""

from flask import Flask, render_template
from flask_socketio import SocketIO
import threading
import time
import RPi.GPIO as GPIO
import cv2

# ── GPIO Pin Definitions ─────────────────────────────────────────────────────
PIR_PIN          = 10
ULTRASONIC_TRIG  = 17
ULTRASONIC_ECHO  = 27
BUTTON_3RD_FLOOR = 13
BUTTON_6TH_FLOOR = 19
RELAY_DOOR       = 22
RELAY_MOTOR_UP   = 23
RELAY_MOTOR_DOWN = 24

# ── GPIO Setup ────────────────────────────────────────────────────────────────
GPIO.setmode(GPIO.BCM)
GPIO.setwarnings(False)

GPIO.setup(PIR_PIN,          GPIO.IN)
GPIO.setup(ULTRASONIC_TRIG,  GPIO.OUT)
GPIO.setup(ULTRASONIC_ECHO,  GPIO.IN)
GPIO.setup(BUTTON_3RD_FLOOR, GPIO.IN, pull_up_down=GPIO.PUD_UP)
GPIO.setup(BUTTON_6TH_FLOOR, GPIO.IN, pull_up_down=GPIO.PUD_UP)
GPIO.setup(RELAY_DOOR,       GPIO.OUT)
GPIO.setup(RELAY_MOTOR_UP,   GPIO.OUT)
GPIO.setup(RELAY_MOTOR_DOWN, GPIO.OUT)

# ── Application State ─────────────────────────────────────────────────────────
current_floor    = 1
requested_floors = set()
elevator_busy    = False

app      = Flask(__name__)
socketio = SocketIO(app)


# ── Web Routes ────────────────────────────────────────────────────────────────
@app.route('/')
def index():
    return render_template('index.html')


# ── Sensor Functions ──────────────────────────────────────────────────────────
def get_distance() -> float:
    """Return distance in centimetres from HC-SR04.
    Returns 999 on timeout (no echo received within 40 ms)."""
    GPIO.output(ULTRASONIC_TRIG, False)
    time.sleep(0.05)
    GPIO.output(ULTRASONIC_TRIG, True)
    time.sleep(0.00001)
    GPIO.output(ULTRASONIC_TRIG, False)

    pulse_start = pulse_end = time.time()

    timeout = time.time() + 0.04
    while GPIO.input(ULTRASONIC_ECHO) == 0:
        pulse_start = time.time()
        if pulse_start > timeout:
            return 999

    timeout = time.time() + 0.04
    while GPIO.input(ULTRASONIC_ECHO) == 1:
        pulse_end = time.time()
        if pulse_end > timeout:
            return 999

    return (pulse_end - pulse_start) * 17150


def detect_face() -> bool:
    """Return True if a face is detected in any of 10 webcam frames."""
    try:
        cascade  = cv2.CascadeClassifier('haarcascade_frontalface_default.xml')
        cap      = cv2.VideoCapture(0)
        detected = False

        for _ in range(10):
            ret, frame = cap.read()
            if not ret:
                continue
            gray  = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            faces = cascade.detectMultiScale(gray, 1.3, 5)
            if len(faces) > 0:
                print("Face detected")
                detected = True
                break
            cv2.waitKey(1000)

        cap.release()
        cv2.destroyAllWindows()
        return detected

    except Exception as exc:
        print(f"Face detection error: {exc}")
        return False


def check_person_presence() -> bool:
    """Sensor fusion: return True when ≥ 2 of 3 sensors confirm presence."""
    pir_detected       = GPIO.input(PIR_PIN) == GPIO.HIGH
    distance           = get_distance()
    ultrasonic_detected = distance < 150          # cm threshold
    face_detected      = detect_face()

    total = int(pir_detected) + int(ultrasonic_detected) + int(face_detected)
    print(f"PIR: {pir_detected}, Ultrasonic: {ultrasonic_detected}, "
          f"Face: {face_detected}, Total: {total}")
    return total >= 2


# ── Elevator Motion ───────────────────────────────────────────────────────────
def move_elevator_to(target_floor: int) -> None:
    global current_floor, elevator_busy

    elevator_busy = True
    print(f"Moving elevator from floor {current_floor} to floor {target_floor}...")

    # Close doors
    socketio.emit('door', {'open': False})
    GPIO.output(RELAY_DOOR, GPIO.LOW)
    time.sleep(1)

    # Drive motor floor-by-floor
    direction = 1 if target_floor > current_floor else -1
    while current_floor != target_floor:
        if direction == 1:
            GPIO.output(RELAY_MOTOR_UP,   GPIO.HIGH)
            GPIO.output(RELAY_MOTOR_DOWN, GPIO.LOW)
        else:
            GPIO.output(RELAY_MOTOR_UP,   GPIO.LOW)
            GPIO.output(RELAY_MOTOR_DOWN, GPIO.HIGH)

        current_floor += direction
        print(f"Elevator at floor {current_floor}")
        socketio.emit('move', {'floor': current_floor})
        time.sleep(2)

    GPIO.output(RELAY_MOTOR_UP,   GPIO.LOW)
    GPIO.output(RELAY_MOTOR_DOWN, GPIO.LOW)

    # Check for person before opening
    print(f"Checking for person presence at floor {target_floor}")
    if check_person_presence():
        print("Person detected. Opening doors.")
        GPIO.output(RELAY_DOOR, GPIO.HIGH)
        socketio.emit('door', {'open': True})
        time.sleep(3)
        print("Closing doors.")
        GPIO.output(RELAY_DOOR, GPIO.LOW)
        socketio.emit('door', {'open': False})
    else:
        print("No person detected. Skipping door open.")

    elevator_busy = False


# ── Background Threads ────────────────────────────────────────────────────────
def button_monitor() -> None:
    """Poll floor-request buttons and add to the queue."""
    global requested_floors
    while True:
        if GPIO.input(BUTTON_3RD_FLOOR) == GPIO.LOW:
            requested_floors.add(3)
            print("3rd floor requested")
            time.sleep(0.5)          # debounce
        if GPIO.input(BUTTON_6TH_FLOOR) == GPIO.LOW:
            requested_floors.add(6)
            print("6th floor requested")
            time.sleep(0.5)
        time.sleep(0.1)


def elevator_control_loop() -> None:
    """Dispatch the elevator to the nearest pending floor."""
    global elevator_busy, requested_floors
    while True:
        if not elevator_busy and requested_floors:
            target = min(requested_floors, key=lambda f: abs(f - current_floor))
            requested_floors.remove(target)
            move_elevator_to(target)
        time.sleep(0.1)


# ── Entry Point ───────────────────────────────────────────────────────────────
if __name__ == '__main__':
    print("Elevator system started. Press Ctrl+C to exit.")
    try:
        threading.Thread(target=button_monitor,        daemon=True).start()
        threading.Thread(target=elevator_control_loop, daemon=True).start()
        socketio.run(app, host='0.0.0.0', port=5000)
    finally:
        GPIO.cleanup()
