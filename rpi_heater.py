import platform
import json
from time import sleep
import subprocess
from threading import Thread, Semaphore
from datetime import datetime
import atexit

import requests
from flask import Flask, request, jsonify, render_template
from flask_cors import CORS

import pigpio

app = Flask(__name__)
app.config['TEMPLATES_AUTO_RELOAD'] = True
CORS(app)


def threaded(fn):
    def wrapper(*args, **kwargs):
        thread = Thread(target=fn, args=args, kwargs=kwargs)
        thread.daemon = True
        thread.start()
        return thread
    return wrapper


class HeaterControl:
    servo_1 = 18
    servo_2 = 19

    lasers = (16, 20, 21)

    buzzer = 26
    buzzer_order = (400, 600, 800)
    buzzer_dutycycle = 25

    dial_interval_width = 335
    servo_1_max = 1680
    servo_1_min = 1250
    servo_2_max = 2500

    dial_min = 4
    dial_mid = 6.5
    dial_max = 8.65
    dial_initial = dial_mid
    enabled_initial = False

    last_use_limit = 1

    def __init__(self) -> None:

        self.mock = platform.system() == 'Darwin'

        if not self.mock:
            try:
                subprocess.run(['sudo', 'pigpiod'],
                               capture_output=True, check=True)
            except subprocess.CalledProcessError as ex:
                if 'Can\'t lock' not in str(ex):
                    raise ex

        self.servo_last_used = [datetime.now(), datetime.now()]
        self.servo_lock = Semaphore()

        self.buzzer_thread = None

        self.state_path = 'state'
        self.was_auto_on = False
        self.load_state()

        if self.mock:
            self.pi = None
        else:
            self.pi = pigpio.pi()
            self.pi.set_mode(self.servo_1, pigpio.OUTPUT)
            self.pi.set_mode(self.servo_2, pigpio.OUTPUT)
            self.set_dial(self.dial)
            self.set_enabled(self.enabled)
            self.deactivate_servos_if_unused()

        self.weather = {}
        if not self.weather:
            print('weather is empty')
        else:
            print('weather is something')

    def load_state(self):
        try:
            with open(self.state_path, 'r') as raw_config:
                self.config = json.loads(raw_config.read())
                self.dial = self.config['dial']
                self.enabled = self.config['enabled']
        except Exception as ex:
            print(str(ex))
            print('Couldn\'t load state. Recreating...')
            self.dial = self.dial_initial
            self.enabled = self.enabled_initial
            with open(self.state_path, 'w') as raw_config:
                raw_config.write(json.dumps(
                    {'dial': self.dial, 'enabled': self.enabled}))

    def _set_dial_in_state(self, value):
        self.dial = value
        with open(self.state_path, 'w') as raw_config:
            raw_config.write(json.dumps(
                {'dial': value, 'enabled': self.enabled}))

    def _set_enabled_in_state(self, enabled):
        self.enabled = enabled
        with open(self.state_path, 'w') as raw_config:
            raw_config.write(json.dumps(
                {'dial': self.dial, 'enabled': enabled}))

    @threaded
    def deactivate_servos_if_unused(self):
        # every 50ms, check if either servo has been used for self.last_use_limit. If not, turn off
        while True:
            self.servo_lock.acquire()
            servo_1_delta = datetime.now() - self.servo_last_used[0]
            if servo_1_delta.total_seconds() > self.last_use_limit:
                # print('deactivate servo 1 after seconds:',
                #       servo_1_delta.total_seconds())
                self.pi.set_servo_pulsewidth(self.servo_1, 0)

            servo_2_delta = datetime.now() - self.servo_last_used[1]
            if servo_2_delta.total_seconds() > self.last_use_limit:
                print('deactivate servo 2 after seconds:',
                      servo_2_delta.total_seconds())
                self.pi.set_servo_pulsewidth(self.servo_2, 0)
            self.servo_lock.release()
            sleep(0.05)

    @threaded
    def update_weather_for_current_time(self):
        # every 30 min, update current weather and evaluate if cold enough + right time to turn on heater
        while True:

            self.weather = requests.get('https://api.weatherapi.com/v1/current.json', params={
                'key': 'c3d94521226d48d0a6b63200211604',
                'q': '-33.775720,151.169162'
            })
            current_temp = self.weather['current']['temp_c']
            print(f'Current temperature: {current_temp}')

            now = datetime.now()
            if now.hour >= 7 and now.hour < 11:
                self.evaluate_temp(current_temp)
            elif now.hour >= 11:  # disable after
                self.set_enabled(False)
                self.was_auto_on = False
            sleep(60 * 30)

    def evaluate_temp(self, current_temp):
        if current_temp < 10:
            self.set_enabled(True)
            self.set_dial(6.5)
        elif current_temp < 12:
            self.set_enabled(True)
            self.set_dial(6.25)
        elif current_temp < 15:
            self.set_enabled(True)
            self.set_dial(6)
        else:
            return
        self.was_auto_on = True

    def set_dial(self, value):
        if self.dial == value:
            print(f'Dial is already {value}')
            return
        value = self.normalise_dial_value(value)
        servo_2_new_value = self.translate(
            value, self.dial_min, self.dial_max, self.servo_2_max - self.dial_interval_width * 5,
            self.servo_2_max)
        print(f'Setting dial to {value}...')
        self.set_servo(self.servo_2, servo_2_new_value)
        self._set_dial_in_state(value)

    def set_enabled(self, enabled):
        if self.enabled == enabled:
            print(f'Enabled is already {enabled}')
            return
        if self.buzzer_thread:
            self.using_buzzer = True
        if enabled:
            print(f'Setting enabled to true...')
            self.set_servo(self.servo_1, self.servo_1_max)
        else:
            print(f'Setting enabled to false...')
            self.set_servo(self.servo_1, self.servo_1_min)
        self.buzzer_thread = self.set_buzzer(enabled)
        self._set_enabled_in_state(enabled)

    def normalise_dial_value(self, value):
        boundary = None
        if value < self.dial_min:
            value = boundary = self.dial_min
        elif value > self.dial_max:
            value = boundary = self.dial_max
        else:
            return value

        print(f'Set dial to {boundary}, out of bounds')
        return value

    @threaded
    def set_buzzer(self, enabled):
        order = list(self.buzzer_order)
        if not enabled:
            order.reverse()

        self.pi.set_PWM_frequency(self.buzzer, order[0])
        self.pi.set_PWM_dutycycle(self.buzzer, self.buzzer_dutycycle)
        sleep(0.2)
        self.pi.set_PWM_frequency(self.buzzer, order[1])
        sleep(0.2)
        self.pi.set_PWM_frequency(self.buzzer, order[2])
        sleep(0.2)
        self.pi.set_PWM_dutycycle(self.buzzer, 0)

    def translate(self, value, inMin, inMax, outMin, outMax):
        return outMin + ((float(value - inMin) / float(inMax - inMin)) * (outMax - outMin))

    def set_servo(self, servo_pin, pulsewidth):
        if servo_pin == self.servo_1:
            self.servo_last_used[0] = datetime.now()
        elif servo_pin == self.servo_2:
            self.servo_last_used[1] = datetime.now()
            print(f'Setting dial to {pulsewidth}...')

        self.servo_lock.acquire()
        self.pi.set_servo_pulsewidth(servo_pin, pulsewidth)
        self.servo_lock.release()

    @threaded
    def run_test(self):
        try:
            self.servo_lock.acquire()
            for _ in range(2):

                self.set_enabled(True)
                sleep(0.5)
                self.set_enabled(False)
                sleep(0.5)

                for i in range(9, 3, -1):
                    self.set_dial(i)
                    sleep(0.5)
                sleep(1)

        except Exception as ex:
            print(str(ex))

        finally:
            print('Ending test')
            self.servo_lock.release()

    def calibrate(self):
        self.set_dial(9)
        self.set_enabled(True)


hc = HeaterControl()


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/state', methods=['GET', 'POST'])
def state():
    if request.method == 'GET':
        return jsonify(dial=hc.dial, enabled=hc.enabled, statusCode=200)
    if request.method == 'POST':
        try:
            request_json = request.json
            if 'dial' in request_json:
                hc.set_dial(request_json['dial'])
            if 'enabled' in request_json:
                hc.set_enabled(request_json['enabled'])
        except Exception as ex:
            return jsonify(exception=str(ex), statusCode=500)
        return jsonify(dial=hc.dial, enabled=hc.enabled, statusCode=202)


@app.route('/test')
def test():
    print('Running test...')
    hc.run_test()
    return 'Running test...'


@app.route('/calibrate')
def calibrate():
    print('Calibrating...')
    hc.calibrate()
    return 'Calibrating...'


def gracefully_handle_exit():
    try:
        hc.pi.stop()
    except:
        pass
    print('Exiting gracefully...')


atexit.register(gracefully_handle_exit)


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080)


# TODO: beep to a ninth, octave, seventh, etc. based on hand distance when detected.
# TODO: to turn on/off, flash hand over. to set temp, hold hand for 1 sec and then adjust height.
# TODO: js slider to adjust temp & on/off. gets current state.
# TODO: adjust heater temp based on room temp. get a temp sensor elsewhere. do some advanced config
