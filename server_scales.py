import socket
import time
import statistics
import re


def parse_weight(data_string):
    """
    Parse weight values from the scale data stream.
    Format: 82.3000= represents 3.28 kg (digits reversed)
            82.300-= represents -3.28 kg (digits reversed, negative)
    """
    weights = []

    # Split by '=' to get individual readings
    readings = data_string.split('=')

    for reading in readings:
        reading = reading.strip()
        if not reading:
            continue

        # Check if it's a negative value (ends with -)
        is_negative = reading.endswith('-')
        if is_negative:
            value_str = reading[:-1]  # Remove the '-' suffix
        else:
            value_str = reading

        try:
            # Reverse the digits to get actual weight
            reversed_str = value_str[::-1]
            weight = float(reversed_str)

            if is_negative:
                weight = -weight

            weights.append(weight)
        except ValueError:
            continue

    return weights


def get_scale_median(host='192.168.88.11', port=8235, duration=1.0):
    """
    Connect to TCP scale server, collect data for specified duration,
    and return the median weight value and last reading.

    Args:
        host: IP address of the scale server
        port: TCP port number
        duration: Time in seconds to collect data

    Returns:
        Tuple of (median_weight, last_weight) in kg, or (None, None) if no valid data received
    """
    all_weights = []

    sock = None
    try:
        # Create TCP socket
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(duration + 2)  # Add buffer to timeout

        print(f"Connecting to {host}:{port}...")
        sock.connect((host, port))
        print("Connected successfully")

        # Wait 1 second after connection before collecting data
        print("Waiting 1 second for scale to stabilize...")
        time.sleep(1.0)

        # Clear any buffered data from the initial second
        sock.setblocking(False)
        try:
            while True:
                sock.recv(1024)
        except BlockingIOError:
            pass  # No more data to clear
        sock.setblocking(True)

        print(f"Collecting data for {duration} second(s)...")
        start_time = time.time()
        received_data = ""

        # Collect data for the specified duration
        while time.time() - start_time < duration:
            try:
                # Receive data in chunks
                data = sock.recv(1024).decode('ascii', errors='ignore')

                if data:
                    received_data += data
                    # Parse weights from received data
                    weights = parse_weight(data)
                    all_weights.extend(weights)
                else:
                    # No data received, connection might be closed
                    break

            except socket.timeout:
                break

        sock.close()

        # Calculate and return median and last weight
        if all_weights:
            median_weight = statistics.median(all_weights)
            last_weight = all_weights[-1]
            #print(f"\nCollected {len(all_weights)} readings")
            #print(f"Min: {min(all_weights):.3f} kg")
            #print(f"Max: {max(all_weights):.3f} kg")
            #print(f"Median: {median_weight:.3f} kg")
            print(f"Last: {last_weight:.3f} kg")
            return median_weight, last_weight
        else:
            print("No valid weight data received")
            return None, None

    except socket.timeout:
        print("Connection timeout")
        if sock:
            try:
                sock.close()
            except Exception:
                pass
        return None, None
    except ConnectionRefusedError:
        print(f"Connection refused. Make sure the scale is online at {host}:{port}")
        return None, None
    except Exception as e:
        print(f"Error: {e}")
        if sock:
            try:
                sock.close()
            except Exception:
                pass
        return None, None


