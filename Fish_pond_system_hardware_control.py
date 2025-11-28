"""
Smart Fish Pond Monitoring System

A comprehensive IoT-based monitoring and control system for aquaculture fish ponds.
This system continuously monitors water quality parameters including temperature, pH,
electrical conductivity (EC), nitrogen, phosphorus, and turbidity.

Key Features:
    - Real-time sensor data collection from RS485 and DS18B20 sensors
    - Multi-threaded architecture for concurrent monitoring, display, and control
    - Automatic water quality assessment with configurable thresholds
    - Visual and audio alerts via LEDs and buzzer
    - Automatic pump control based on water quality status
    - SMS notifications for critical conditions
    - Cloud data logging to ThingSpeak platform
    - Local backup system for offline data storage
    - Network connectivity monitoring and auto-recovery

Hardware Components:
    - Raspberry Pi (main controller)
    - RS485 multi-parameter water quality sensor
    - DS18B20 temperature sensor
    - 16x2 LCD display (I2C)
    - GSM module for SMS alerts
    - GPIO-controlled LEDs, buzzer, and water pump

Author: Smart Fish Pond Development Team
Version: 5.14
"""

import os
import glob
import time
import serial
import random  # Used for exponential backoff in retry logic
import RPi.GPIO as GPIO
from datetime import datetime
import minimalmodbus  # For RS485 Modbus communication
from RPLCD.i2c import CharLCD  # For I2C LCD display control
import threading  # For concurrent operations
from collections import deque  # For efficient historical data storage
import requests  # For HTTP requests to ThingSpeak API
import csv
import json
import logging
from pathlib import Path
import sys
import socket  # For network connectivity checks
import subprocess  # For system-level network commands
import re  # For text parsing in LCD display

# ============================================================================
# CONFIGURATION AND INITIALIZATION
# ============================================================================

# Get directory where the script is located (for relative file paths)
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# ============================================================================
# LOGGING CONFIGURATION
# ============================================================================

# Configure logging with proper path handling
# Logs are written to both file and console for debugging and monitoring
LOG_FILE = os.path.join(SCRIPT_DIR, 'pond_monitor.log')

# Ensure log directory exists before creating log file
os.makedirs(os.path.dirname(LOG_FILE) if os.path.dirname(LOG_FILE) else '.', exist_ok=True)

# Set up logging with timestamp, level, and message format
# FileHandler: Persistent log storage for troubleshooting
# StreamHandler: Real-time console output for monitoring
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ============================================================================
# CONFIGURATION CONSTANTS
# ============================================================================

# Configuration file path (JSON format for easy editing)
CONFIG_FILE = os.path.join(SCRIPT_DIR, 'pond_config.json')

# Mapping of water quality status text to numeric codes for ThingSpeak
# ThingSpeak charts require numeric values, so we encode status as integers
QUALITY_STATUS_CODES = {
    "GOOD": 0,      # Normal operating conditions
    "WARNING": 1,   # Parameters outside optimal range, monitoring required
    "CRITICAL": 2   # Immediate action required to prevent fish mortality
}

# Default configuration values (can be overridden by pond_config.json)
DEFAULT_CONFIG = {
    # Sensor reading intervals (seconds)
    "TEMP_READ_INTERVAL": 15,      # How often to read sensors
    "LCD_REFRESH_INTERVAL": 5,     # How often to update LCD display
    
    # Pump control durations (seconds) - different modes for different severity levels
    "PUMP_RUN_DURATION": {
        "SHORT": 120,   # 2 minutes - for pH emergencies
        "NORMAL": 180,  # 5 minutes - standard aeration/circulation
        "LONG": 240     # 10 minutes - for temperature emergencies
    },
    
    # Alert and notification settings
    "SMS_COOLDOWN": 120,           # Minimum time between SMS alerts (5 minutes)
    "CRITICAL_DURATION": 120,      # How long condition must persist before SMS (5 minutes)
    "HISTORY_SIZE": 5,             # Number of readings to keep for averaging/trending
    
    # ThingSpeak cloud logging configuration
    "THINGSPEAK": {
        "URL": "https://api.thingspeak.com/update",
        "API_KEY": "",
        "SEND_INTERVAL": 60,       # Minimum 15 seconds (ThingSpeak rate limit)
        "BACKUP_FILE": os.path.join(SCRIPT_DIR, 'pond_thingspeak_backup.csv'),
        "MAX_RETRIES": 3,          # Retry attempts for failed uploads
        "RETRY_DELAY": 5,          # Initial retry delay (seconds)
        "MAX_RETRY_DELAY": 60      # Maximum retry delay (exponential backoff cap)
    },
    
    # GPIO pin assignments (BCM numbering)
    "GPIO": {
        "TURBIDITY_PIN": 17,       # Digital input for turbidity sensor
        "BUZZER_PIN": 18,          # PWM output for buzzer
        "BLUE_LED_PIN": 27,        # Output for GOOD status indicator
        "YELLOW_LED_PIN": 22,      # Output for WARNING status indicator
        "RED_LED_PIN": 5,          # Output for CRITICAL status indicator
        "PUMP_PIN": 16             # Output for water pump relay control
    },
    
    # Sensor communication settings
    "SENSORS": {
        "RS485_PORT": '/dev/ttyUSB0',  # Serial port for RS485 sensor
        "RS485_SLAVE_ID": 1,           # Modbus slave address
        "GSM_PORT": "/dev/serial0",    # Serial port for GSM module
        "GSM_BAUDRATE": 9600          # Communication speed
    },
    
    # Phone numbers for SMS alerts (international format with country code)
    "PHONE_NUMBERS": ["", ""]
}

def check_network_connectivity():
    """
    Check if the device has internet connectivity.
    
    Uses Google's public DNS (8.8.8.8) as a connectivity test endpoint.
    This is a lightweight check that doesn't require actual HTTP requests.
    
    Returns:
        bool: True if network is reachable, False otherwise
    """
    try:
        # Attempt to resolve Google's DNS IP address
        # If this succeeds, basic network connectivity exists
        socket.gethostbyname('8.8.8.8')
        return True
    except socket.gaierror:
        logger.warning("Network connectivity check failed: Name resolution failed.")
        return False
    except Exception as e:
        logger.error(f"Error during network check: {e}")
        return False

def restart_network_interface():
    """
    Attempt to restart the network interface to recover from connectivity issues.
    
    This function performs a soft reset of the wireless interface by:
    1. Bringing the interface down
    2. Bringing it back up
    3. Restarting the network service (NetworkManager or networking)
    
    This is useful when the system detects persistent network failures and
    attempts automatic recovery before falling back to backup storage.
    
    Returns:
        bool: True if connectivity is restored, False otherwise
    """
    try:
        logger.info("Attempting to restart network interface...")
        
        # Bring wireless interface down (disconnect)
        subprocess.run(["sudo", "ip", "link", "set", "wlan0", "down"], 
                      stderr=subprocess.PIPE, stdout=subprocess.PIPE, timeout=10)
        time.sleep(2)  # Allow interface to fully disconnect
        
        # Bring wireless interface back up (reconnect)
        subprocess.run(["sudo", "ip", "link", "set", "wlan0", "up"], 
                      stderr=subprocess.PIPE, stdout=subprocess.PIPE, timeout=10)
        time.sleep(5)  # Allow interface to initialize
        
        # Restart network management service
        # Try NetworkManager first (common on modern RPi OS), fallback to networking
        try:
            subprocess.run(["sudo", "systemctl", "restart", "NetworkManager"], 
                          timeout=10, check=False)
        except:
            subprocess.run(["sudo", "systemctl", "restart", "networking"], 
                          timeout=10, check=False)
        
        logger.info("Network interface restart commands issued. Waiting 15 seconds...")
        time.sleep(15)  # Allow time for DHCP and connection establishment
        
        # Verify connectivity was restored
        if check_network_connectivity():
            logger.info("✅ Network connectivity restored after restart.")
            return True
        else:
            logger.warning("Network connectivity still down after restart attempt.")
            return False
    except Exception as e:
        logger.error(f"Failed to restart network interface: {e}")
        return False

def load_config():
    """
    Load configuration from JSON file or create default configuration file.
    
    This function implements a merge strategy: user-defined values in the config
    file override defaults, but nested dictionaries are merged rather than replaced.
    This allows partial configuration updates without losing default values.
    
    Returns:
        dict: Configuration dictionary with user overrides applied to defaults
    """
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r') as f:
                user_config = json.load(f)
                merged_config = DEFAULT_CONFIG.copy()
                
                # Merge user config with defaults
                # Nested dicts are merged, top-level values are replaced
                for key, value in user_config.items():
                    if key in merged_config and isinstance(merged_config[key], dict) and isinstance(value, dict):
                        merged_config[key].update(value)  # Merge nested dictionaries
                    else:
                        merged_config[key] = value  # Replace top-level values
                        
                logger.info(f"✅ Configuration loaded from {CONFIG_FILE}")
                return merged_config
        except Exception as e:
            logger.error(f"Error loading config: {e}. Using defaults.")
    else:
        # Config file doesn't exist - create it with default values
        try:
            with open(CONFIG_FILE, 'w') as f:
                json.dump(DEFAULT_CONFIG, f, indent=2)
            logger.info(f"✅ Created default config file at {CONFIG_FILE}")
        except Exception as e:
            logger.error(f"Error creating config file: {e}")

    return DEFAULT_CONFIG

# Load configuration at module import time
config = load_config()

# ============================================================================
# UTILITY FUNCTIONS
# ============================================================================

def ensure_directory_exists(file_path):
    """
    Ensure the directory for the given file path exists.
    
    Creates parent directories if they don't exist. This is useful for
    ensuring backup files and logs can be written even if directories
    haven't been created yet.
    
    Args:
        file_path (str): Full path to a file
        
    Returns:
        bool: True if directory exists or was created, False on error
    """
    directory = os.path.dirname(file_path)
    if directory and not os.path.exists(directory):
        try:
            os.makedirs(directory)
            logger.info(f"Created directory: {directory}")
            return True
        except Exception as e:
            logger.error(f"Error creating directory {directory}: {e}")
            return False
    return True

def ensure_backup_file_exists():
    """
    Create the ThingSpeak backup CSV file if it doesn't exist.
    
    The backup file stores sensor readings when network connectivity is lost.
    This ensures no data is lost during outages. The file is created with
    a proper CSV header for easy parsing when flushing data later.
    
    Returns:
        bool: True if backup file exists or was created, False on error
    """
    backup_file = config.get('THINGSPEAK', {}).get('BACKUP_FILE')
    if not backup_file:
        logger.error("BACKUP_FILE not found in configuration.")
        return False
        
    if not os.path.exists(backup_file):
        logger.info(f"Backup file not found. Creating new one at: {backup_file}")
        try:
            ensure_directory_exists(backup_file)
            with open(backup_file, 'w', newline='') as f:
                writer = csv.writer(f)
                # Write CSV header with all sensor parameters
                writer.writerow(["timestamp", "temperature", "ph", "ec", "nitrogen",
                              "phosphorus", "turbidity", "quality_score", "quality_status"])
            logger.info(f"✅ Successfully created new backup file.")
            return True
        except Exception as e:
            logger.error(f"Failed to create backup file: {e}")
            return False
    return True

# ============================================================================
# MAIN MONITORING CLASS
# ============================================================================

class SmartFishPondMonitor:
    """
    Main monitoring and control class for the Smart Fish Pond System.
    
    This class manages all aspects of the monitoring system including:
    - Sensor data collection and processing
    - Water quality assessment
    - Visual/audio alerts
    - Automatic pump control
    - SMS notifications
    - Cloud data logging
    - Multi-threaded operation
    
    The system uses a multi-threaded architecture where different threads
    handle monitoring, display updates, pump control, and cloud logging
    concurrently for responsive operation.
    """
    
    def __init__(self):
        """
        Initialize the Smart Fish Pond Monitor system.
        
        Sets up all hardware interfaces, initializes data structures,
        and starts background threads for continuous operation.
        """
        # ====================================================================
        # SYSTEM STATE INITIALIZATION
        # ====================================================================
        # Centralized state dictionary for thread-safe access to system status
        self.state = {
            'running': True,
            'pump': {
                'should_run': False,
                'is_running': False,
                'mode': "OFF",
                'start_time': 0
            },
            'indicators': {
                'last_status': 'GOOD',
                'lcd_screen': 0,
                'last_lcd_content': "",
                'lcd_error_count': 0,
                'lcd_available': True
            },
            'sms': {
                'last_sent_time': 0
            },
            'critical': {
                'start_time': None,
                'sustained': False,
                'alert_sent': False
            },
            'thingspeak': {
                'last_sent_time': 0,
                'last_reading': {},
                'connection_errors': 0,
                'last_network_check': 0,
                'network_error_count': 0
            },
            'sensor_errors': {
                'rs485_error_count': 0,
                'last_successful_read': None
            }
        }

        # ====================================================================
        # THREAD SAFETY AND MONITORING
        # ====================================================================
        # Thread locks for safe concurrent access to shared resources
        # Minimal locking strategy: only lock when modifying shared state
        self.state_lock = threading.Lock()  # Protects self.state dictionary
        self.gsm_lock = threading.Lock()    # Protects GSM module access (serial port)

        # Watchdog timestamps for monitoring thread health
        # Each thread updates its timestamp periodically; watchdog checks for stuck threads
        self.thread_watchdog = {
            'monitor': time.time(),      # Main sensor reading loop
            'display': time.time(),      # LCD update loop
            'pump': time.time(),         # Pump control loop
            'thingspeak': time.time()   # Cloud logging loop
        }

        # ====================================================================
        # HISTORICAL DATA STORAGE
        # ====================================================================
        # Use deque (double-ended queue) for efficient rolling window storage
        # Automatically discards oldest values when maxlen is reached
        # This provides moving averages and trend analysis without manual array management
        self.history = {
            'temp': deque(maxlen=config.get('HISTORY_SIZE', 5)),
            'ph': deque(maxlen=config.get('HISTORY_SIZE', 5)),
            'ec': deque(maxlen=config.get('HISTORY_SIZE', 5)),
            'nitrogen': deque(maxlen=config.get('HISTORY_SIZE', 5)),
            'phosphorus': deque(maxlen=config.get('HISTORY_SIZE', 5)),
            'turbidity': deque(maxlen=config.get('HISTORY_SIZE', 5))
        }

        # ====================================================================
        # GPIO INITIALIZATION
        # ====================================================================
        try:
            # Use BCM pin numbering (Broadcom chip pin numbers)
            GPIO.setmode(GPIO.BCM)
            
            # Configure input pin for turbidity sensor (pull-up resistor enabled)
            GPIO.setup(config.get('GPIO', {}).get('TURBIDITY_PIN', 17), GPIO.IN, pull_up_down=GPIO.PUD_UP)
            
            # Configure output pins for status LEDs
            GPIO.setup(config.get('GPIO', {}).get('BLUE_LED_PIN', 27), GPIO.OUT)   # GOOD status
            GPIO.setup(config.get('GPIO', {}).get('YELLOW_LED_PIN', 22), GPIO.OUT) # WARNING status
            GPIO.setup(config.get('GPIO', {}).get('RED_LED_PIN', 5), GPIO.OUT)     # CRITICAL status
            
            # Configure output pins for buzzer and pump
            GPIO.setup(config.get('GPIO', {}).get('BUZZER_PIN', 18), GPIO.OUT)
            GPIO.setup(config.get('GPIO', {}).get('PUMP_PIN', 16), GPIO.OUT)

            # Initialize PWM for buzzer (1000 Hz frequency)
            # PWM allows volume control via duty cycle
            self.pwm_buzzer = GPIO.PWM(config.get('GPIO', {}).get('BUZZER_PIN', 18), 1000)
            self.pwm_buzzer.start(0)  # Start with 0% duty cycle (silent)

            # Initialize all LEDs to OFF and pump to OFF (HIGH = relay off)
            self.set_leds(0, 0, 0)
            GPIO.output(config.get('GPIO', {}).get('PUMP_PIN', 16), GPIO.HIGH)
            logger.info("✅ GPIO initialized successfully")
        except Exception as e:
            logger.error(f"GPIO initialization failed: {e}")
            raise

        # ====================================================================
        # PERIPHERAL DEVICE INITIALIZATION
        # ====================================================================
        # Initialize all sensor and communication interfaces
        self.lcd = self._init_lcd()                    # I2C LCD display
        self.rs485_instrument = self._init_rs485()     # RS485 water quality sensor
        self.device_file = self._init_ds18b20()        # DS18B20 temperature sensor
        self.gsm = self._init_gsm()                    # GSM module for SMS
        
        # Test ThingSpeak connection at startup to verify API key and connectivity
        logger.info("Testing ThingSpeak connection...")
        self.test_thingspeak_connection()

        # ====================================================================
        # BACKGROUND THREAD INITIALIZATION
        # ====================================================================
        # Create daemon threads (automatically terminate when main program exits)
        # Each thread handles a specific aspect of system operation
        self.monitor_thread = threading.Thread(target=self.monitor_loop, name="Monitor", daemon=True)
        self.display_thread = threading.Thread(target=self.display_loop, name="Display", daemon=True)
        self.pump_thread = threading.Thread(target=self.pump_control_loop, name="Pump", daemon=True)
        self.thingspeak_thread = threading.Thread(target=self.thingspeak_loop, name="ThingSpeak", daemon=True)
        self.watchdog_thread = threading.Thread(target=self.watchdog_loop, name="Watchdog", daemon=True)

        # Start all background threads
        for t in [self.monitor_thread, self.display_thread, self.pump_thread,
                 self.thingspeak_thread, self.watchdog_thread]:
            t.start()

    def test_thingspeak_connection(self):
        """
        Test ThingSpeak API connection at system startup.
        
        Sends a minimal test payload to verify:
        - API key is valid
        - Network connectivity exists
        - ThingSpeak service is accessible
        
        Returns:
            bool: True if connection test succeeds, False otherwise
        """
        test_payload = {
            "api_key": config.get('THINGSPEAK', {}).get('API_KEY'),
            "field1": 25.0  # Test value for temperature field
        }
        
        try:
            response = requests.get(
                config.get('THINGSPEAK', {}).get('URL'),
                params=test_payload,
                timeout=10
            )
            
            if response.status_code == 200:
                result = response.text.strip()
                # ThingSpeak returns entry ID (>0) on success, 0 on failure
                if result.isdigit() and int(result) > 0:
                    logger.info(f"✅ ThingSpeak connection test PASSED (Entry: {result})")
                    return True
                else:
                    logger.error(f"❌ ThingSpeak returned: '{result}' (0 = rate limit or invalid key)")
                    return False
            else:
                logger.error(f"❌ HTTP Error: {response.status_code}")
                return False
                
        except Exception as e:
            logger.error(f"❌ Connection test failed: {e}")
            return False

    def _init_lcd(self):
        """
        Initialize the I2C LCD display.
        
        Attempts to connect to LCD via I2C using PCF8574 I2C expander
        at address 0x27. Implements retry logic for reliability.
        
        Returns:
            CharLCD: LCD object if successful, None if initialization fails
        """
        max_retries = 3
        for attempt in range(max_retries):
            try:
                # PCF8574 is common I2C-to-parallel converter for LCDs
                # Address 0x27 is default for many LCD modules
                lcd = CharLCD('PCF8574', 0x27)
                lcd.clear()
                lcd.write_string("Initializing...")
                logger.info("✅ LCD initialized")
                return lcd
            except Exception as e:
                logger.error(f"LCD initialization failed (attempt {attempt + 1}/{max_retries}): {e}")
                if attempt < max_retries - 1:
                    time.sleep(2)  # Wait before retry

        # Mark LCD as unavailable - system continues without display
        self.state['indicators']['lcd_available'] = False
        logger.warning("LCD unavailable - continuing without display")
        return None

    def _init_rs485(self):
        """
        Initialize RS485 Modbus communication with water quality sensor.
        
        Configures serial port parameters for Modbus RTU protocol:
        - 9600 baud (standard for many Modbus devices)
        - 8 data bits, no parity, 1 stop bit (8N1)
        - 1 second timeout for register reads
        
        Returns:
            Instrument: minimalmodbus Instrument object if successful, None otherwise
        """
        try:
            # Create Modbus instrument with port and slave ID
            instrument = minimalmodbus.Instrument(
                config.get('SENSORS', {}).get('RS485_PORT', '/dev/ttyUSB0'),
                config.get('SENSORS', {}).get('RS485_SLAVE_ID', 1)
            )
            # Configure serial communication parameters
            instrument.serial.baudrate = 9600
            instrument.serial.bytesize = 8
            instrument.serial.parity = minimalmodbus.serial.PARITY_NONE
            instrument.serial.stopbits = 1
            instrument.serial.timeout = 1  # 1 second timeout for reads
            logger.info("✅ RS485 sensor initialized")
            return instrument
        except Exception as e:
            logger.error(f"❌ RS485 initialization failed: {e}")
            return None

    def _init_ds18b20(self):
        """
        Initialize DS18B20 1-Wire temperature sensor.
        
        Loads required kernel modules and locates the sensor device file.
        DS18B20 sensors appear in /sys/bus/w1/devices/ with prefix '28-'.
        
        Returns:
            str: Path to sensor device file (w1_slave) if found, None otherwise
        """
        try:
            # Load kernel modules for 1-Wire GPIO and temperature support
            os.system('modprobe w1-gpio')
            os.system('modprobe w1-therm')
            
            # DS18B20 devices have family code 28 in their address
            base_dir = '/sys/bus/w1/devices/'
            device_folders = glob.glob(base_dir + '28-*')
            
            if not device_folders:
                logger.error("No DS18B20 temperature sensor found. Check wiring and kernel modules.")
                return None
                
            # Use first found sensor (most systems have one)
            device_folder = device_folders[0]
            device_file = device_folder + '/w1_slave'  # Temperature data file
            logger.info("✅ DS18B20 temperature sensor initialized")
            return device_file
        except (IndexError, Exception) as e:
            logger.error(f"❌ DS18B20 initialization failed: {e}")
            return None

    def _init_gsm(self):
        try:
            ser = serial.Serial(port=config.get('SENSORS', {}).get('GSM_PORT', "/dev/serial0"),
                              baudrate=config.get('SENSORS', {}).get('GSM_BAUDRATE', 9600), timeout=5)
            self.send_at_command(ser, "AT", 1)
            self.send_at_command(ser, "ATE0", 1)
            self.send_at_command(ser, "AT+CMGF=1", 1)
            logger.info("✅ GSM module initialized")
            return ser
        except Exception as e:
            logger.error(f"❌ GSM initialization failed: {e}")
            return None

    def send_at_command(self, ser, command, delay=1):
        try:
            ser.write((command + "\r\n").encode())
            time.sleep(delay)
            return ser.read_all().decode(errors="ignore").strip()
        except Exception as e:
            logger.error(f"Error sending AT command '{command}': {e}")
            return ""

    def read_ds18b20_temp(self):
        if not self.device_file:
            return None
        try:
            with open(self.device_file, 'r') as f:
                lines = f.readlines()
            if lines and lines[0].strip().endswith('YES') and 't=' in lines[1]:
                equals_pos = lines[1].find('t=')
                if equals_pos != -1:
                    temp_c = float(lines[1][equals_pos + 2:]) / 1000.0
                    if -10 < temp_c < 60:
                        return temp_c
        except Exception as e:
            logger.debug(f"Error reading DS18B20: {e}")
        return None

    def read_turbidity(self):
        try:
            return GPIO.input(config.get('GPIO', {}).get('TURBIDITY_PIN', 17)) == 0
        except:
            return None

    def read_rs485_sensor(self):
        if not self.rs485_instrument:
            self.state['sensor_errors']['rs485_error_count'] += 1
            return {}

        try:
            data = {}
            registers = {
                'temperature': 19, 'ph': 13, 'ec': 7,
                'nitrogen': 4, 'phosphorus': 5, 'potassium': 6
            }

            temp_value = self._read_register_with_retry(registers['temperature'])
            data['temperature'] = temp_value

            for param, reg in registers.items():
                if param == 'temperature':
                    continue
                value = self._read_register_with_retry(reg)
                if param == 'ph':
                    data['ph_raw'] = value
                    if value is not None and value > 0.5:
                        data[param] = self.calculate_ph(value, data.get('temperature', 25))
                    else:
                        data[param] = None
                else:
                    data[param] = value

            self.state['sensor_errors']['rs485_error_count'] = 0
            self.state['sensor_errors']['last_successful_read'] = time.time()
            return data

        except Exception as e:
            self.state['sensor_errors']['rs485_error_count'] += 1
            logger.error(f"RS485 sensor read error #{self.state['sensor_errors']['rs485_error_count']}: {e}")

            if self.state['sensor_errors']['rs485_error_count'] >= 5:
                logger.warning("Multiple RS485 errors, attempting to reinitialize sensor")
                self.rs485_instrument = self._init_rs485()
                self.state['sensor_errors']['rs485_error_count'] = 0

            return {}

    def _read_register_with_retry(self, register, retries=2):
        for attempt in range(retries):
            try:
                value = self.rs485_instrument.read_register(register, 1, functioncode=3)
                return value
            except Exception as e:
                logger.debug(f"RS485 read attempt {attempt + 1} failed for reg {register}: {e}")
                if attempt < retries - 1:
                    time.sleep(0.1)
        return None

    def calculate_ph(self, raw_value, temperature):
        """
        Convert raw pH sensor reading to calibrated pH value.
        
        Applies temperature compensation to account for sensor drift
        with temperature changes. Reference temperature is 25°C.
        
        Args:
            raw_value (float): Raw sensor reading (typically 0-45)
            temperature (float): Current water temperature in Celsius
            
        Returns:
            float: Calibrated pH value (0-14), or None if invalid reading
        """
        if raw_value is None or raw_value <= 0.5:
            return None
        # Convert raw value to pH (calibration factor from sensor datasheet)
        ph = raw_value / 3.13
        # Temperature compensation: -0.01 pH per degree from 25°C
        temp_compensation = (temperature - 25) * 0.01
        return ph - temp_compensation

    def combine_temperatures(self, rs485_temp, ds18b20_temp):
        """
        Combine temperature readings from two sensors using weighted average.
        
        Uses both RS485 and DS18B20 sensors for improved accuracy and redundancy.
        RS485 reading gets higher weight (0.6) as it's typically more stable,
        while DS18B20 (0.4) provides independent verification.
        
        Args:
            rs485_temp (float): Temperature from RS485 sensor in Celsius
            ds18b20_temp (float): Temperature from DS18B20 sensor in Celsius
            
        Returns:
            float: Combined temperature value, or single sensor value if only one available
        """
        if rs485_temp is not None and ds18b20_temp is not None:
            # Weighted average: RS485 (60%) + DS18B20 (40%)
            return 0.6 * rs485_temp + 0.4 * ds18b20_temp
        elif rs485_temp is not None:
            return rs485_temp  # Fallback to RS485 if DS18B20 unavailable
        elif ds18b20_temp is not None:
            return ds18b20_temp  # Fallback to DS18B20 if RS485 unavailable
        return None

    def update_historical_data(self, data, ds18b20_temp):
        combined_temp = self.combine_temperatures(data.get('temperature'), ds18b20_temp)
        if combined_temp is not None:
            self.history['temp'].append(combined_temp)
        if data.get('ph') is not None:
            self.history['ph'].append(data['ph'])
        if data.get('ec') is not None:
            self.history['ec'].append(data['ec'])
        if data.get('nitrogen') is not None:
            self.history['nitrogen'].append(data['nitrogen'])
        if data.get('phosphorus') is not None:
            self.history['phosphorus'].append(data['phosphorus'])

        turbidity = self.read_turbidity()
        if turbidity is not None:
            self.history['turbidity'].append(1 if turbidity else 0)

    def get_average(self, history):
        if not history:
            return None
        return sum(history) / len(history)

    def get_trend(self, history):
        """
        Analyze trend direction from historical data.
        
        Compares first half vs second half of historical readings to determine
        if values are increasing, decreasing, or stable. Uses adaptive threshold
        based on magnitude of values to avoid false positives from noise.
        
        Args:
            history (deque): Historical readings for a parameter
            
        Returns:
            str: "INCREASING", "DECREASING", or "STABLE"
        """
        if len(history) < 3:
            return "STABLE"  # Need at least 3 readings for trend analysis
            
        mid = len(history) // 2
        # Calculate average of first half vs second half
        first_half_avg = sum(list(history)[:mid]) / mid
        second_half_avg = sum(list(history)[mid:]) / (len(history) - mid)
        diff = second_half_avg - first_half_avg
        
        # Adaptive threshold: 10% of value or minimum 0.2 units
        # Prevents false trends from small fluctuations
        threshold = max(0.1 * abs(first_half_avg), 0.2)

        if diff > threshold:
            return "INCREASING"
        elif diff < -threshold:
            return "DECREASING"
        return "STABLE"

    def get_water_quality_status(self, data, ds18b20_temp):
        """
        Assess overall water quality status based on multiple parameters.
        
        This is the core decision-making function that evaluates all sensor readings
        against aquaculture-specific thresholds. It calculates a risk score and
        determines overall status (GOOD/WARNING/CRITICAL).
        
        Thresholds are calibrated for fish pond aquaculture where higher nutrient
        levels (100-150 mg/kg) are normal compared to drinking water standards.
        
        Args:
            data (dict): Current RS485 sensor readings (temperature, pH, EC, etc.)
            ds18b20_temp (float): Temperature from DS18B20 sensor in Celsius
            
        Returns:
            dict: Status dictionary containing:
                - overall (str): 'GOOD', 'WARNING', or 'CRITICAL'
                - score (int): Risk score (0-100+, higher = worse)
                - alerts (list): List of alert messages
                - recommendations (set): Suggested actions
                - trends (dict): Trend analysis for each parameter
        """
        self.update_historical_data(data, ds18b20_temp)

        status = {
            'overall': 'GOOD', 'score': 0, 'alerts': [],
            'recommendations': set(), 'trends': {}
        }

        avg_temp = self.get_average(self.history['temp'])
        avg_ph = self.get_average(self.history['ph'])
        avg_ec = self.get_average(self.history['ec'])
        avg_nitrogen = self.get_average(self.history['nitrogen'])
        avg_phosphorus = self.get_average(self.history['phosphorus'])
        turbidity_ratio = sum(self.history['turbidity']) / len(self.history['turbidity']) if self.history['turbidity'] else 0

        status['trends']['temperature'] = self.get_trend(self.history['temp'])
        status['trends']['ph'] = self.get_trend(self.history['ph'])
        status['trends']['nitrogen'] = self.get_trend(self.history['nitrogen'])
        status['trends']['phosphorus'] = self.get_trend(self.history['phosphorus'])

        # Check if we have any valid data
        if not any([avg_temp, avg_ph, avg_ec, avg_nitrogen, avg_phosphorus]):
            status['alerts'].append('⚠️ No valid sensor data available')
            status['score'] += 50
            status['overall'] = 'WARNING'
            return status

        # === pH ASSESSMENT (MOST CRITICAL) ===
        if avg_ph is None:
            status['alerts'].append('⚠️ pH sensor not in water or faulty')
            status['score'] += 30
        elif avg_ph < 5.5:  # RAISED from 6.0 - truly dangerous
            status['score'] += 50
            status['alerts'].append(f"🚨 CRITICAL: pH {avg_ph:.1f} is dangerously acidic")
            status['recommendations'].add('URGENT: Add agricultural lime (CaCO₃) immediately')
        elif avg_ph < 6.0:  # RAISED from 6.5 - warning zone
            status['score'] += 25
            status['alerts'].append(f"⚠️ WARNING: pH {avg_ph:.1f} is acidic")
            status['recommendations'].add('Consider adding agricultural lime gradually')
        elif avg_ph < 6.5:  # NEW - monitoring zone
            status['score'] += 10
            status['alerts'].append(f"ℹ️ pH {avg_ph:.1f} is slightly low but acceptable")
        elif avg_ph > 9.5:  # RAISED from 9.0 - truly dangerous
            status['score'] += 50
            status['alerts'].append(f"🚨 CRITICAL: pH {avg_ph:.1f} is dangerously alkaline")
            status['recommendations'].add('URGENT: Perform large water change')
        elif avg_ph > 9.0:  # RAISED from 8.5 - warning zone
            status['score'] += 25
            status['alerts'].append(f"⚠️ WARNING: pH {avg_ph:.1f} is too alkaline")
            status['recommendations'].add('Perform partial water change')
        elif avg_ph > 8.5:  # Monitoring zone
            status['score'] += 10
            status['alerts'].append(f"ℹ️ pH {avg_ph:.1f} is slightly high but acceptable")

        # === TEMPERATURE ASSESSMENT ===
        if avg_temp is not None:
            if avg_temp < 12:  # LOWERED from 15 - truly critical for most fish
                status['score'] += 40
                status['alerts'].append(f"🚨 CRITICAL: Temperature {avg_temp:.1f}°C is too cold")
                status['recommendations'].add('Add pond heater immediately')
            elif avg_temp < 18:  # LOWERED from 20 - just warning
                status['score'] += 15
                status['alerts'].append(f"⚠️ Temperature {avg_temp:.1f}°C is cool")
            elif avg_temp > 35:  # RAISED from 32 - truly dangerous
                status['score'] += 40
                status['alerts'].append(f"🚨 CRITICAL: Temperature {avg_temp:.1f}°C is too hot")
                status['recommendations'].add('Add shade and emergency aeration')
            elif avg_temp > 30:  # RAISED from 28 - just warning
                status['score'] += 15
                status['alerts'].append(f"⚠️ Temperature {avg_temp:.1f}°C is warm")
                status['recommendations'].add('Monitor closely and increase aeration')

        # === ELECTRICAL CONDUCTIVITY (Salinity/TDS) ===
        if avg_ec is not None and avg_ec > 2000:  # RAISED from 1500
            status['score'] += 30
            status['alerts'].append(f"⚠️ EC {avg_ec:.0f} μS/cm is high")
            status['recommendations'].add('Consider diluting with fresh water')

        # === TURBIDITY ===
        if turbidity_ratio > 0.8:  # RAISED from 0.7
            status['score'] += 20
            status['alerts'].append(f"⚠️ Water is consistently turbid")
            status['recommendations'].add('Check filter and consider water change')

        # === NITROGEN ASSESSMENT (REVISED - more realistic) ===
        # Note: 100+ mg/kg nitrogen is actually common in aquaculture systems
        # Critical levels are typically 200+ mg/kg
        if avg_nitrogen is not None:
            if avg_nitrogen > 200:  # NEW CRITICAL threshold
                status['score'] += 35
                status['alerts'].append(f"🚨 CRITICAL: Nitrogen extremely high: {avg_nitrogen:.1f} mg/kg")
                status['recommendations'].add('URGENT: Stop feeding and perform large water change')
            elif avg_nitrogen > 150:  # NEW WARNING threshold (was 5!)
                status['score'] += 20
                status['alerts'].append(f"⚠️ WARNING: Nitrogen high: {avg_nitrogen:.1f} mg/kg")
                status['recommendations'].add('Reduce feeding and increase water changes')
            elif avg_nitrogen > 100:  # NEW MONITORING threshold
                status['score'] += 5
                status['alerts'].append(f"ℹ️ Nitrogen elevated: {avg_nitrogen:.1f} mg/kg (monitor)")

        # === PHOSPHORUS ASSESSMENT (REVISED - more realistic) ===
        # Similar to nitrogen - 100+ mg/kg is common in productive systems
        if avg_phosphorus is not None:
            if avg_phosphorus > 200:  # NEW CRITICAL threshold
                status['score'] += 35
                status['alerts'].append(f"🚨 CRITICAL: Phosphorus extremely high: {avg_phosphorus:.1f} mg/kg")
                status['recommendations'].add('URGENT: Stop feeding and perform large water change')
            elif avg_phosphorus > 150:  # NEW WARNING threshold (was 15!)
                status['score'] += 20
                status['alerts'].append(f"⚠️ WARNING: Phosphorus high: {avg_phosphorus:.1f} mg/kg")
                status['recommendations'].add('Reduce feeding and increase water changes')
            elif avg_phosphorus > 100:  # NEW MONITORING threshold
                status['score'] += 5
                status['alerts'].append(f"ℹ️ Phosphorus elevated: {avg_phosphorus:.1f} mg/kg (monitor)")

        # === FINAL STATUS DETERMINATION (ADJUSTED THRESHOLDS) ===
        if status['score'] >= 80:  # RAISED from 70
            status['overall'] = 'CRITICAL'
        elif status['score'] >= 40:  # KEPT at 40
            status['overall'] = 'WARNING'
        else:
            status['overall'] = 'GOOD'

        return status

    def set_leds(self, blue, yellow, red):
        try:
            GPIO.output(config.get('GPIO', {}).get('BLUE_LED_PIN', 27), blue)
            GPIO.output(config.get('GPIO', {}).get('YELLOW_LED_PIN', 22), yellow)
            GPIO.output(config.get('GPIO', {}).get('RED_LED_PIN', 5), red)
        except Exception as e:
            logger.debug(f"LED control error: {e}")

    def update_indicators(self, status):
        # Minimal locking - only lock state updates
        self.set_leds(0, 0, 0)
        
        try:
            self.pwm_buzzer.ChangeDutyCycle(0)
        except:
            pass

        if status['overall'] == 'GOOD':
            self.set_leds(1, 0, 0)
            with self.state_lock:
                self.state['pump']['should_run'] = False
                self.state['pump']['mode'] = "OFF"
                
        elif status['overall'] == 'WARNING':
            self.set_leds(0, 1, 0)
            try:
                threading.Timer(0.2, lambda: self.pwm_buzzer.ChangeDutyCycle(50)).start()
                threading.Timer(0.4, lambda: self.pwm_buzzer.ChangeDutyCycle(0)).start()
            except:
                pass
            with self.state_lock:
                self.state['pump']['should_run'] = False
                
        elif status['overall'] == 'CRITICAL':
            self.set_leds(0, 0, 1)
            try:
                threading.Timer(1.0, lambda: self.pwm_buzzer.ChangeDutyCycle(50)).start()
                threading.Timer(1.2, lambda: self.pwm_buzzer.ChangeDutyCycle(0)).start()
            except:
                pass

            with self.state_lock:
                if not self.state['pump']['is_running']:
                    self.state['pump']['should_run'] = True
                    self._determine_pump_mode(status)

    def _determine_pump_mode(self, status):
        avg_temp = self.get_average(self.history['temp'])
        avg_ph = self.get_average(self.history['ph'])
        turbidity_ratio = sum(self.history['turbidity']) / len(self.history['turbidity']) if self.history['turbidity'] else 0

        if avg_temp is not None and (avg_temp < 15 or avg_temp > 32):
            self.state['pump']['mode'] = "LONG"
        elif avg_ph is not None and (avg_ph < 6.0 or avg_ph > 9.0):
            self.state['pump']['mode'] = "SHORT"
        elif turbidity_ratio > 0.7:
            self.state['pump']['mode'] = "NORMAL"
        else:
            self.state['pump']['mode'] = "NORMAL"

    def handle_critical_state(self, status):
        current_time = time.time()

        if status['overall'] == 'CRITICAL':
            with self.state_lock:
                if not self.state['critical']['sustained']:
                    self.state['critical']['start_time'] = current_time
                    self.state['critical']['sustained'] = True
                    self.state['critical']['alert_sent'] = False
                    logger.info("⚠️ Critical condition detected. Starting 5-minute timer before SMS alert.")

                elif (not self.state['critical']['alert_sent'] and
                      current_time - self.state['critical']['start_time'] > config.get('CRITICAL_DURATION', 300)):
                    logger.info("🚨 Sustained critical condition confirmed. Sending SMS alert.")
                    self._send_sms_message(status)
                    self.state['sms']['last_sent_time'] = current_time
                    self.state['critical']['alert_sent'] = True

        else:
            with self.state_lock:
                if self.state['critical']['sustained']:
                    logger.info("✅ Condition recovered. Resetting critical state.")
                    self.state['critical']['start_time'] = None
                    self.state['critical']['sustained'] = False
                    self.state['critical']['alert_sent'] = False

    def _send_sms_message(self, status):
        if not self.gsm:
            logger.error("GSM module not available for SMS")
            return

        message = f"🚨 POND ALERT ({status['overall']})\n"
        message += f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n"
        message += "Critical Issues:\n" + "\n".join(status['alerts'][:3])
        if status['recommendations']:
            message += f"\n\nActions:\n" + "\n".join(list(status['recommendations'])[:2])

        with self.gsm_lock:
            for phone in config.get('PHONE_NUMBERS', []):
                try:
                    self.gsm.write(f'AT+CMGS="{phone}"\r\n'.encode())
                    time.sleep(1)
                    self.gsm.write(message.encode())
                    time.sleep(0.5)
                    self.gsm.write(b'\x1A')
                    time.sleep(5)
                    final_response = self.gsm.read_all().decode(errors="ignore")
                    if "+CMGS:" in final_response:
                        logger.info(f"✅ SMS sent to {phone}")
                    else:
                        logger.error(f"❌ SMS failed to {phone}: {final_response.strip()}")
                except Exception as e:
                    logger.error(f"Error sending SMS to {phone}: {e}")

    def pump_control_loop(self):
        while self.state['running']:
            try:
                with self.state_lock:
                    should_activate = self.state['pump']['should_run'] and not self.state['pump']['is_running']
                    is_running = self.state['pump']['is_running']
                    pump_mode = self.state['pump']['mode']
                    start_time = self.state['pump']['start_time']
                
                if should_activate:
                    logger.info(f"▶️ Activating PUMP in {pump_mode} mode.")
                    GPIO.output(config.get('GPIO', {}).get('PUMP_PIN', 16), GPIO.LOW)
                    with self.state_lock:
                        self.state['pump']['is_running'] = True
                        self.state['pump']['start_time'] = time.time()
                        self.state['pump']['should_run'] = False

                if is_running:
                    run_duration = config.get('PUMP_RUN_DURATION', {}).get(pump_mode, 300)
                    if time.time() - start_time > run_duration:
                        logger.info(f"⏹️ {pump_mode} pump cycle complete. Turning PUMP OFF.")
                        GPIO.output(config.get('GPIO', {}).get('PUMP_PIN', 16), GPIO.HIGH)
                        with self.state_lock:
                            self.state['pump']['is_running'] = False
                            self.state['pump']['mode'] = "OFF"

                self.thread_watchdog['pump'] = time.time()
                time.sleep(1)
            except Exception as e:
                logger.error(f"Error in pump_control_loop: {e}")
                self.thread_watchdog['pump'] = time.time()
                time.sleep(5)

    def _update_lcd_content(self, status):
        if not self.state['indicators']['lcd_available'] or not self.lcd:
            return

        try:
            line1 = ""
            line2 = ""

            # For WARNING/CRITICAL, show status prominently
            if status['overall'] in ['WARNING', 'CRITICAL']:
                # First line: Status indicator (16 chars exactly)
                if status['overall'] == 'WARNING':
                    line1 = "!   WARNING   !"
                else:
                    line1 = "!! CRITICAL  !!"

                # Second line: Extract and display the main issue (16 chars exactly)
                if status['alerts']:
                    alert = status['alerts'][0]
                    # Remove emoji and clean text
                    alert_clean = ''.join(c for c in alert if ord(c) < 128)
                    alert_clean = alert_clean.replace('CRITICAL:', '').replace('DANGER:', '').replace('WARNING:', '').replace('ℹ️', '').strip()

                    # Parse and format specific issues to fit exactly 16 chars
                    if 'pH' in alert_clean:
                        # Match patterns like "pH 6.3" or "pH: 6.3"
                        ph_match = re.search(r'pH[:\s]+([\d.]+)', alert_clean)
                        if ph_match:
                            ph_val = float(ph_match.group(1))
                            if 'acidic' in alert_clean.lower() or 'dangerously acidic' in alert_clean.lower():
                                line2 = f"pH:{ph_val:4.1f} ACIDIC "
                            elif 'alkaline' in alert_clean.lower() or 'dangerously alkaline' in alert_clean.lower():
                                line2 = f"pH:{ph_val:4.1f} ALKALIN"
                            elif 'slightly low' in alert_clean.lower():
                                line2 = f"pH:{ph_val:4.1f} LOW    "
                            elif 'slightly high' in alert_clean.lower():
                                line2 = f"pH:{ph_val:4.1f} HIGH   "
                            else:
                                line2 = f"pH:{ph_val:4.1f} Problem"
                        else:
                            line2 = "pH sensor issue "

                    elif 'Temperature' in alert_clean:
                        temp_match = re.search(r'([\d.]+).?C', alert_clean)
                        if temp_match:
                            temp_val = float(temp_match.group(1))
                            if 'cold' in alert_clean.lower():
                                line2 = f"T:{temp_val:4.1f}C  COLD  "
                            elif 'cool' in alert_clean.lower():
                                line2 = f"T:{temp_val:4.1f}C  COOL  "
                            elif 'hot' in alert_clean.lower():
                                line2 = f"T:{temp_val:4.1f}C   HOT  "
                            elif 'warm' in alert_clean.lower():
                                line2 = f"T:{temp_val:4.1f}C  WARM  "
                            else:
                                line2 = f"T:{temp_val:4.1f}C   BAD "
                        else:
                            line2 = "Temp issue      "

                    elif 'Nitrogen' in alert_clean:
                        # Match patterns like "Nitrogen high: 109.6 mg/kg" or "Nitrogen extremely high: 109.6 mg/kg"
                        n_match = re.search(r'([\d.]+)\s*mg/kg', alert_clean)
                        if n_match:
                            n_val = float(n_match.group(1))
                            if 'extremely high' in alert_clean.lower() or 'critical' in alert_clean.lower():
                                line2 = f"N:{n_val:6.1f} CRIT  "
                            elif 'high' in alert_clean.lower() or 'warning' in alert_clean.lower():
                                line2 = f"N:{n_val:6.1f} HIGH  "
                            elif 'elevated' in alert_clean.lower():
                                line2 = f"N:{n_val:6.1f} ELEV  "
                            else:
                                line2 = f"N:{n_val:6.1f} HIGH  "
                        else:
                            line2 = "Nitrogen too HI "

                    elif 'Phosphorus' in alert_clean:
                        # Match patterns like "Phosphorus high: 117.2 mg/kg" or "Phosphorus extremely high: 117.2 mg/kg"
                        p_match = re.search(r'([\d.]+)\s*mg/kg', alert_clean)
                        if p_match:
                            p_val = float(p_match.group(1))
                            if 'extremely high' in alert_clean.lower() or 'critical' in alert_clean.lower():
                                line2 = f"P:{p_val:6.1f} CRIT  "
                            elif 'high' in alert_clean.lower() or 'warning' in alert_clean.lower():
                                line2 = f"P:{p_val:6.1f} HIGH  "
                            elif 'elevated' in alert_clean.lower():
                                line2 = f"P:{p_val:6.1f} ELEV  "
                            else:
                                line2 = f"P:{p_val:6.1f} HIGH  "
                        else:
                            line2 = "Phosphorus HIGH "

                    elif 'EC' in alert_clean:
                        ec_match = re.search(r'EC\s+([\d.]+)', alert_clean)
                        if ec_match:
                            ec_val = float(ec_match.group(1))
                            line2 = f"EC:{ec_val:5.0f} HIGH   "
                        else:
                            line2 = "EC too high     "

                    elif 'turbid' in alert_clean.lower():
                        line2 = "WATER TURBID    "

                    elif 'sensor' in alert_clean.lower() and 'pH' in alert_clean:
                        line2 = "pH sensor fault "
                    elif 'sensor' in alert_clean.lower():
                        line2 = "Sensor fault    "
                    elif 'No valid' in alert_clean:
                        line2 = "No sensor data  "
                    else:
                        line2 = alert_clean[:16].ljust(16)
                else:
                    line2 = "Check system    "

            else:
                # GOOD status - Rotate through 4 screens
                screen = self.state['indicators']['lcd_screen'] % 4

                if screen == 0:
                    # Screen 1: Temperature and pH
                    temp = self.get_average(self.history['temp'])
                    ph = self.get_average(self.history['ph'])

                    if temp is not None and ph is not None:
                        line1 = f"T:{temp:4.1f}C  pH:{ph:4.1f} "
                        
                        # Line 2: Show trends
                        temp_trend = status['trends'].get('temperature', 'STABLE')
                        ph_trend = status['trends'].get('ph', 'STABLE')

                        # Both stable
                        if temp_trend == "STABLE" and ph_trend == "STABLE":
                            line2 = "TREND: STABLE   "
                        # Both same direction
                        elif temp_trend == "INCREASING" and ph_trend == "INCREASING":
                            line2 = "TREND: UP   UP  "
                        elif temp_trend == "DECREASING" and ph_trend == "DECREASING":
                            line2 = "TREND: DOWN DOWN"
                        # Different directions
                        elif temp_trend == "INCREASING" and ph_trend == "DECREASING":
                            line2 = "TREND: UP   DOWN"
                        elif temp_trend == "DECREASING" and ph_trend == "INCREASING":
                            line2 = "TREND: DOWN UP  "
                        # One stable, one moving
                        elif temp_trend == "INCREASING":
                            line2 = "TREND: UP   --  "
                        elif temp_trend == "DECREASING":
                            line2 = "TREND: DOWN --  "
                        elif ph_trend == "INCREASING":
                            line2 = "TREND: --   UP  "
                        elif ph_trend == "DECREASING":
                            line2 = "TREND: --   DOWN"
                        else:
                            line2 = "TREND: STABLE   "
                    else:
                        line1 = "Temperature & pH"
                        line2 = "No data         "

                elif screen == 1:
                    # Screen 2: EC and Water clarity
                    ec = self.get_average(self.history['ec'])
                    turbidity_ratio = sum(self.history['turbidity']) / len(self.history['turbidity']) if self.history['turbidity'] else 0

                    if ec is not None:
                        turbidity_status = "TURBID" if turbidity_ratio > 0.5 else "CLEAR "
                        line1 = f"EC:{ec:5.0f} uS/cm  "
                        line2 = f"WATER: {turbidity_status:6} "
                    else:
                        line1 = "EC & Turbidity  "
                        line2 = "No data         "

                elif screen == 2:
                    # Screen 3: Nitrogen and Phosphorus
                    n = self.get_average(self.history['nitrogen'])
                    p = self.get_average(self.history['phosphorus'])

                    if n is not None and p is not None:
                        line1 = f"N:{n:6.1f} mg/kg  "
                        line2 = f"P:{p:6.1f} mg/kg  "
                    else:
                        line1 = "Nitrogen & Phos."
                        line2 = "No data         "

                else:
                    # Screen 4: Overall status and score
                    status_text = status['overall']
                    score = status['score']
                    line1 = f"Status: {status_text:7} "
                    line2 = f"Score: {score:3}/100   "

            # Ensure exactly 16 chars per line
            line1 = line1[:16].ljust(16)
            line2 = line2[:16].ljust(16)
            
            # Combine for comparison
            content = line1 + line2

            # Only update LCD if content has changed
            if content != self.state['indicators']['last_lcd_content']:
                try:
                    self.lcd.clear()
                    time.sleep(0.01)  # Small delay after clear
                    # Write line by line for 16x2 LCD - explicitly set cursor positions
                    self.lcd.cursor_pos = (0, 0)
                    self.lcd.write_string(line1)
                    time.sleep(0.01)  # Small delay between lines
                    self.lcd.cursor_pos = (1, 0)
                    self.lcd.write_string(line2)
                    
                    self.state['indicators']['last_lcd_content'] = content
                    logger.debug(f"LCD Updated - Line1: '{line1}' Line2: '{line2}'")
                except Exception as lcd_err:
                    logger.error(f"LCD write error: {lcd_err}")
                    raise

            self.state['indicators']['lcd_error_count'] = 0

        except Exception as e:
            self.state['indicators']['lcd_error_count'] += 1
            logger.error(f"LCD error #{self.state['indicators']['lcd_error_count']}: {e}")

            if self.state['indicators']['lcd_error_count'] >= 5:
                logger.warning("Multiple LCD errors, marking as unavailable")
                self.state['indicators']['lcd_available'] = False

    def display_loop(self):
        while self.state['running']:
            try:
                last_status = self.state.get('last_status_full')
                if last_status:
                    self._update_lcd_content(last_status)
                
                self.state['indicators']['lcd_screen'] += 1
                self.thread_watchdog['display'] = time.time()
                time.sleep(config.get('LCD_REFRESH_INTERVAL', 5))
            except Exception as e:
                logger.error(f"Error in display_loop: {e}")
                self.thread_watchdog['display'] = time.time()
                time.sleep(5)

    def validate_sensor_data(self, data):
        valid_ranges = {
            'temperature': (-40, 80),
            'ph': (0, 14),
            'ec': (0, 5000),
            'nitrogen': (0, 200),  # Increased to handle high readings
            'phosphorus': (0, 200),  # Increased to handle high readings
            'quality_score': (0, 150)
        }

        out_of_range = False

        for param, (min_val, max_val) in valid_ranges.items():
            value = data.get(param)
            if value is not None:
                try:
                    val_float = float(value)
                    if not (min_val <= val_float <= max_val):
                        logger.warning(f"{param} out of range: {val_float} (expected {min_val}-{max_val})")
                        out_of_range = True
                except (ValueError, TypeError) as e:
                    logger.warning(f"{param} validation error: {value} - {e}")
                    out_of_range = True

        quality_status = data.get('quality_status')
        if quality_status and quality_status not in ['GOOD', 'WARNING', 'CRITICAL']:
            logger.debug(f"Invalid quality_status: {quality_status}")
            data['quality_status'] = 'GOOD'

        return not out_of_range

    def has_any_valid_data(self, data):
        return any(v is not None for k, v in data.items()
                   if k not in ['quality_score', 'quality_status'])

    def send_to_thingspeak(self, data, timestamp):
        """
        Upload sensor data to ThingSpeak cloud platform.
        
        Implements retry logic with exponential backoff for network reliability.
        Converts quality status text to numeric codes for ThingSpeak chart compatibility.
        Saves data to backup file if upload fails.
        
        ThingSpeak Field Mapping:
            field1: Temperature (°C)
            field2: pH
            field3: Electrical Conductivity (μS/cm)
            field4: Nitrogen (mg/kg)
            field5: Phosphorus (mg/kg)
            field6: Turbidity (0=clear, 1=turbid)
            field7: Quality Score (0-100+)
            field8: Quality Status Code (0=GOOD, 1=WARNING, 2=CRITICAL)
        
        Args:
            data (dict): Sensor readings and quality assessment
            timestamp (str): ISO format timestamp for backup file
            
        Returns:
            bool: True if upload successful, False otherwise
        """
        if not config.get('THINGSPEAK', {}).get('API_KEY'):
            logger.warning("ThingSpeak API key not configured")
            return False

        if not self.validate_sensor_data(data):
            logger.debug("Data validation failed. Skipping send.")
            return False

        # Build payload with API key and sensor data
        payload = {"api_key": config.get('THINGSPEAK', {}).get('API_KEY')}
        
        # Map sensor data to ThingSpeak fields (only include non-None values)
        if data.get('temperature') is not None:
            payload['field1'] = data['temperature']
        if data.get('ph') is not None:
            payload['field2'] = data['ph']
        if data.get('ec') is not None:
            payload['field3'] = data['ec']
        if data.get('nitrogen') is not None:
            payload['field4'] = data['nitrogen']
        if data.get('phosphorus') is not None:
            payload['field5'] = data['phosphorus']
        if data.get('turbidity') is not None:
            payload['field6'] = 1 if data.get('turbidity') else 0  # Convert boolean to 0/1
        if data.get('quality_score') is not None:
            payload['field7'] = data['quality_score']
        
        # Convert quality status text to numeric code for ThingSpeak charts
        # ThingSpeak charts require numeric values, not text strings
        if data.get('quality_status') is not None:
            status_text = str(data['quality_status']).upper()
            status_code = QUALITY_STATUS_CODES.get(status_text, -1)  # -1 if unknown status
            payload['field8'] = status_code
            logger.debug(f"Sending status '{status_text}' as code {status_code} to Field 8")

        max_retries = config.get('THINGSPEAK', {}).get('MAX_RETRIES', 3)
        initial_delay = config.get('THINGSPEAK', {}).get('RETRY_DELAY', 5)
        max_delay = config.get('THINGSPEAK', {}).get('MAX_RETRY_DELAY', 60)

        for attempt in range(max_retries):
            if time.time() - self.state['thingspeak']['last_network_check'] > 30:
                self.state['thingspeak']['last_network_check'] = time.time()
                
                if not check_network_connectivity():
                    logger.warning(f"⚠️ Network is down. Saving to backup. (Attempt {attempt+1}/{max_retries})")
                    self.save_thingspeak_backup(timestamp, data)
                    self.state['thingspeak']['network_error_count'] += 1
                    
                    if self.state['thingspeak']['network_error_count'] % 3 == 0:
                        restart_network_interface()
                        time.sleep(5)

                    if not check_network_connectivity():
                        logger.error("❌ Failed to restore network connectivity.")
                        break

            try:
                response = requests.get(
                    config.get('THINGSPEAK', {}).get('URL'), 
                    params=payload, 
                    timeout=10
                )
                
                if response.status_code == 200:
                    result = response.text.strip()
                    if result.isdigit() and int(result) > 0:
                        logger.info(
                            f"✅ ThingSpeak upload (Entry: {result}) - "
                            f"Status: {data.get('quality_status', 'N/A')} "
                            f"(code {payload.get('field8', 'N/A')})"
                        )
                        self.state['thingspeak']['connection_errors'] = 0
                        self.state['thingspeak']['network_error_count'] = 0
                        return True
                    else:
                        logger.warning(f"⚠️ ThingSpeak returned '{result}' (rate limit or invalid data)")
                        self.save_thingspeak_backup(timestamp, data)
                        return False
                else:
                    logger.error(f"❌ HTTP Error: {response.status_code}")
                    self.save_thingspeak_backup(timestamp, data)

            except requests.exceptions.Timeout:
                logger.warning(f"⏱️ Timeout (Attempt {attempt+1}/{max_retries})")
                self.save_thingspeak_backup(timestamp, data)

            except requests.exceptions.ConnectionError as e:
                logger.error(f"🌐 Network error (Attempt {attempt+1}/{max_retries}): {str(e)[:80]}")
                self.save_thingspeak_backup(timestamp, data)

            except requests.exceptions.RequestException as e:
                logger.error(f"❌ Request failed (Attempt {attempt+1}/{max_retries}): {str(e)[:80]}")
                self.save_thingspeak_backup(timestamp, data)
                
            except Exception as e:
                logger.error(f"❌ Unexpected error: {e}")
                self.save_thingspeak_backup(timestamp, data)

            if attempt < max_retries - 1:
                delay = min(max_delay, initial_delay * (2 ** attempt)) + random.uniform(0, 2)
                logger.info(f"⏳ Retrying in {delay:.1f} seconds...")
                time.sleep(delay)

        self.state['thingspeak']['connection_errors'] += 1
        logger.error(f"❌ Failed to send to ThingSpeak after {max_retries} attempts.")
        return False

    def save_thingspeak_backup(self, timestamp, data):
        backup_file = config.get('THINGSPEAK', {}).get('BACKUP_FILE')
        if not backup_file:
            logger.error("Backup file path not configured.")
            return

        ensure_directory_exists(backup_file)
        header_needed = not os.path.exists(backup_file)
        
        try:
            with open(backup_file, "a", newline="") as f:
                writer = csv.writer(f)
                if header_needed:
                    writer.writerow(["timestamp", "temperature", "ph", "ec", "nitrogen",
                                   "phosphorus", "turbidity", "quality_score", "quality_status"])
                writer.writerow([
                    timestamp,
                    data.get('temperature'),
                    data.get('ph'),
                    data.get('ec'),
                    data.get('nitrogen'),
                    data.get('phosphorus'),
                    data.get('turbidity'),
                    data.get('quality_score'),
                    data.get('quality_status')
                ])
            logger.debug(f"💾 Backup saved to {backup_file}")
        except Exception as e:
            logger.error(f"Error saving ThingSpeak backup: {e}")

    def flush_thingspeak_backup(self):
        backup_file = config.get('THINGSPEAK', {}).get('BACKUP_FILE')
        if not backup_file or not os.path.exists(backup_file):
            return

        rows_to_keep = []
        try:
            with open(backup_file, "r", newline="") as f:
                reader = csv.reader(f)
                headers = next(reader, None)
                for row in reader:
                    if len(row) >= 9:
                        try:
                            # Helper function to safely convert values
                            def safe_convert(value, is_numeric=True):
                                if not value or value.strip() == '':
                                    return None
                                if is_numeric:
                                    try:
                                        return float(value)
                                    except ValueError:
                                        return None
                                return value

                            row_dict = {
                                'timestamp': row[0],
                                'temperature': safe_convert(row[1]),
                                'ph': safe_convert(row[2]),
                                'ec': safe_convert(row[3]),
                                'nitrogen': safe_convert(row[4]),
                                'phosphorus': safe_convert(row[5]),
                                'turbidity': safe_convert(row[6]),
                                'quality_score': safe_convert(row[7]),
                                'quality_status': safe_convert(row[8], is_numeric=False)
                            }
                            rows_to_keep.append(row_dict)
                        except (ValueError, IndexError) as e:
                            logger.warning(f"Skipping invalid backup row: {e}")
                            continue
        except Exception as e:
            logger.error(f"Error reading backup file: {e}")
            return

        if not rows_to_keep:
            try:
                os.remove(backup_file)
                logger.info("🗑️ Empty backup file removed")
            except OSError:
                pass
            return

        logger.info(f"📤 Attempting to flush {len(rows_to_keep)} backup entries...")
        still_failed = []

        for row in rows_to_keep:
            # Create data dict, excluding None values
            data = {k: v for k, v in row.items()
                    if k != 'timestamp' and v is not None}

            # Skip if no valid data
            if not self.has_any_valid_data(data):
                logger.warning(f"Skipping backup entry with no valid data: {row['timestamp']}")
                continue

            success = self.send_to_thingspeak(data, row['timestamp'])

            if not success:
                still_failed.append(row)
            else:
                logger.info(f"✅ Flushed backup: {row['timestamp']}")

            time.sleep(16)  # ThingSpeak requires 15 sec minimum

        if still_failed:
            try:
                with open(backup_file, "w", newline="") as f:
                    writer = csv.writer(f)
                    writer.writerow(["timestamp", "temperature", "ph", "ec", "nitrogen",
                                  "phosphorus", "turbidity", "quality_score", "quality_status"])
                    for row in still_failed:
                        writer.writerow([
                            row['timestamp'],
                            row.get('temperature', ''),
                            row.get('ph', ''),
                            row.get('ec', ''),
                            row.get('nitrogen', ''),
                            row.get('phosphorus', ''),
                            row.get('turbidity', ''),
                            row.get('quality_score', ''),
                            row.get('quality_status', '')
                        ])
                logger.info(f"💾 {len(still_failed)} entries remain in backup")
            except Exception as e:
                logger.error(f"Error writing backup file: {e}")
        else:
            try:
                os.remove(backup_file)
                logger.info("✅ All backup data sent to ThingSpeak successfully")
            except OSError:
                pass

    def thingspeak_loop(self):
        while self.state['running']:
            try:
                current_time = time.time()
                send_interval = config.get('THINGSPEAK', {}).get('SEND_INTERVAL', 60)
                
                if current_time - self.state['thingspeak']['last_sent_time'] >= send_interval:
                    data = {
                        'temperature': self.get_average(self.history['temp']),
                        'ph': self.get_average(self.history['ph']),
                        'ec': self.get_average(self.history['ec']),
                        'nitrogen': self.get_average(self.history['nitrogen']),
                        'phosphorus': self.get_average(self.history['phosphorus']),
                        'turbidity': sum(self.history['turbidity']) / len(self.history['turbidity']) if self.history['turbidity'] else 0,
                        'quality_score': self.state.get('last_status_full', {}).get('score', 0),
                        'quality_status': self.state.get('last_status_full', {}).get('overall', 'GOOD')
                    }

                    if self.has_any_valid_data(data):
                        # Try to flush backup first
                        try:
                            self.flush_thingspeak_backup()
                        except Exception as e:
                            logger.error(f"Error flushing backup: {e}")

                        # Send current reading
                        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        if self.send_to_thingspeak(data, timestamp):
                            self.state['thingspeak']['last_sent_time'] = current_time
                    else:
                        logger.debug("Waiting for valid sensor data...")

                self.thread_watchdog['thingspeak'] = time.time()
                time.sleep(5)
                
            except Exception as e:
                logger.error(f"Error in thingspeak_loop: {e}")
                self.thread_watchdog['thingspeak'] = time.time()
                time.sleep(10)

    def watchdog_loop(self):
        while self.state['running']:
            try:
                current_time = time.time()
                timeout = 120  # Increased timeout to 2 minutes

                for thread_name, last_update in self.thread_watchdog.items():
                    if current_time - last_update > timeout:
                        logger.error(f"⚠️ Thread '{thread_name}' appears to be stuck!")

                time.sleep(30)
            except Exception as e:
                logger.error(f"Error in watchdog_loop: {e}")
                time.sleep(30)

    def monitor_loop(self):
        first_cycle = True

        while self.state['running']:
            try:
                rs485_data = self.read_rs485_sensor()
                ds18b20_temp = self.read_ds18b20_temp()

                if not rs485_data:
                    if first_cycle:
                        print("\n" + "="*60)
                        print(f"  POND MONITORING REPORT - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
                        print("="*60)
                        print("\n❌ ERROR: Failed to read from RS485 sensor. Check wiring and power.")
                        print("Waiting for sensor data...")
                        first_cycle = False
                    time.sleep(config.get('TEMP_READ_INTERVAL', 15))
                    continue

                first_cycle = False
                self.update_historical_data(rs485_data, ds18b20_temp)
                status = self.get_water_quality_status(rs485_data, ds18b20_temp)

                self.state['last_status_full'] = status
                self.state['indicators']['last_status'] = status['overall']

                print("\n" + "="*60)
                print(f"  POND MONITORING REPORT - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
                print("="*60)

                print("\n--- SENSOR READINGS ---")
                print(f"DS18B20 Temp:      {ds18b20_temp:.1f}°C" if ds18b20_temp is not None else "DS18B20 Temp:      N/A")
                print(f"RS485 Temp:        {rs485_data.get('temperature'):.1f}°C" if rs485_data.get('temperature') is not None else "RS485 Temp:        N/A")
                print(f"Combined Temp:     {self.get_average(self.history['temp']):.1f}°C" if self.get_average(self.history['temp']) is not None else "Combined Temp:     N/A")

                turbidity = self.read_turbidity()
                print(f"Turbidity:         {'TURBID' if turbidity else 'CLEAR'}" if turbidity is not None else "Turbidity:         N/A")

                print("\n--- RS485 PROBE DATA (UNITS: mg/kg, μS/cm) ---")
                print(f"pH:                {rs485_data.get('ph'):.1f}" if rs485_data.get('ph') is not None else "pH:                N/A")
                print(f"EC:                {rs485_data.get('ec'):.0f} μS/cm" if rs485_data.get('ec') is not None else "EC:                N/A")
                print(f"Nitrogen (N):      {rs485_data.get('nitrogen'):.1f} mg/kg" if rs485_data.get('nitrogen') is not None else "Nitrogen (N):      N/A")
                print(f"Phosphorus (P):    {rs485_data.get('phosphorus'):.1f} mg/kg" if rs485_data.get('phosphorus') is not None else "Phosphorus (P):    N/A")
                print(f"Potassium (K):     {rs485_data.get('potassium'):.0f} mg/kg" if rs485_data.get('potassium') is not None else "Potassium (K):     N/A")

                print("\n--- HISTORICAL AVERAGES ---")
                print(f"Avg Temp:          {self.get_average(self.history['temp']):.1f}°C" if self.get_average(self.history['temp']) is not None else "Avg Temp:          N/A")
                print(f"Avg pH:            {self.get_average(self.history['ph']):.1f}" if self.get_average(self.history['ph']) is not None else "Avg pH:            N/A")
                print(f"Avg EC:            {self.get_average(self.history['ec']):.0f} μS/cm" if self.get_average(self.history['ec']) is not None else "Avg EC:            N/A")
                print(f"Avg Nitrogen:      {self.get_average(self.history['nitrogen']):.1f} mg/kg" if self.get_average(self.history['nitrogen']) is not None else "Avg Nitrogen:      N/A")
                print(f"Avg Phosphorus:    {self.get_average(self.history['phosphorus']):.1f} mg/kg" if self.get_average(self.history['phosphorus']) is not None else "Avg Phosphorus:    N/A")
                turbidity_ratio = sum(self.history['turbidity']) / len(self.history['turbidity']) if self.history['turbidity'] else 0
                print(f"Turbidity Ratio:   {turbidity_ratio:.0%}" if self.history['turbidity'] else "Turbidity Ratio:   N/A")

                print("\n--- TRENDS ---")
                print(f"Temperature Trend:  {status['trends'].get('temperature', 'STABLE')}")
                print(f"pH Trend:          {status['trends'].get('ph', 'STABLE')}")
                print(f"Nitrogen Trend:    {status['trends'].get('nitrogen', 'STABLE')}")
                print(f"Phosphorus Trend:  {status['trends'].get('phosphorus', 'STABLE')}")

                print("\n--- SYSTEM ASSESSMENT ---")
                print(f"Overall Status:    {status['overall']} (Score: {status['score']}/100)")
                if status['alerts']:
                    print("Alerts Triggered:")
                    for alert in status['alerts']:
                        print(f"  - {alert}")
                if status['recommendations']:
                    print("Recommendations:")
                    for rec in status['recommendations']:
                        print(f"  - {rec}")

                self.handle_critical_state(status)
                self.update_indicators(status)

                self.thread_watchdog['monitor'] = time.time()
                time.sleep(config.get('TEMP_READ_INTERVAL', 15))
                
            except KeyboardInterrupt:
                self.state['running'] = False
                break
            except Exception as e:
                logger.error(f"Error in monitor_loop: {e}")
                self.thread_watchdog['monitor'] = time.time()
                time.sleep(5)

    def cleanup(self):
        logger.info("Starting cleanup...")
        self.state['running'] = False

        for t in [self.monitor_thread, self.display_thread, self.pump_thread,
                 self.thingspeak_thread, self.watchdog_thread]:
            if t.is_alive():
                try:
                    t.join(timeout=3)
                except:
                    pass

        self.set_leds(0, 0, 0)
        if self.pwm_buzzer:
            try:
                self.pwm_buzzer.stop()
            except:
                pass
        
        if self.lcd:
            try:
                self.lcd.clear()
                self.lcd.write_string("System stopped")
            except:
                pass

        GPIO.output(config.get('GPIO', {}).get('PUMP_PIN', 16), GPIO.HIGH)
        logger.info("Pump turned OFF.")

        if self.gsm:
            try:
                self.gsm.close()
                logger.info("GSM module closed.")
            except:
                pass

        GPIO.cleanup()
        logger.info("✅ System cleanup complete")

def main():
    logger.info("="*60)
    logger.info("Smart Fish Pond Monitoring System v5.14 (Fixed)")
    logger.info("="*60)
    
    ensure_backup_file_exists()

    try:
        monitor = SmartFishPondMonitor()
        logger.info("✅ System initialized. Monitoring started. Press Ctrl+C to stop.")
        while monitor.state['running']:
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("\n⏹️ Stopping system...")
        monitor.cleanup()
    except Exception as e:
        logger.error(f"❌ Fatal error: {e}")
        try:
            monitor.cleanup()
        except:
            logger.error("Cleanup failed")

if __name__ == "__main__":

    main()
