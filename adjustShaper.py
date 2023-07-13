import datetime
import json
import influxDB2
import keyboard
import requests
import time

from dateutil import tz
from multiprocessing import Process
from multiprocessing import Queue
from pythonping import ping
from requests.auth import HTTPBasicAuth
from threading import Thread, Event

shutdown_pinger_event = Event()
shutdown_influx_write_event = Event()

bandwidth_change_queue = Queue()
influx_utility = influxDB2.InfluxDB2Utility()

#### Opnsense Data ###########################
opnsense_user = '<api_usr>'
opnsense_key = '<api_key>'

base_url = 'https://opnsense/'

interfaces_url = base_url + 'api/diagnostics/traffic/interface'
pipe_search_url = base_url + 'api/trafficshaper/settings/searchPipes/'
get_pipe_url = base_url + 'api/trafficshaper/settings/getPipe/'
update_pipe_url = base_url + 'api/trafficshaper/settings/setPipe/'
shaper_reconfigure_url = base_url + 'api/trafficshaper/service/reconfigure'

# Set auth data for opnsense api calls.
auth = HTTPBasicAuth(opnsense_user, opnsense_key)

max_pipe_value = 200
min_pipe_value = 5

to_zone = tz.tzlocal()
##############################################

class PingerThread(Thread):
    def __init__(self):
        Thread.__init__(self)
        self.ping = {
            'ping': -999,
            'time': datetime.datetime.now().astimezone(to_zone) 
        }
        
    def run(self):
        while True:
            cloudflare_result = ping('1.1.1.1', count = 2).rtt_avg_ms
            google_result = ping('8.8.8.8', count = 2).rtt_avg_ms
            ping_result = {
                'ping': (cloudflare_result + google_result)/2,
                'time': datetime.datetime.now().astimezone(to_zone)
            }
            
            self.ping = ping_result
            
            if shutdown_pinger_event.is_set():
                break
            time.sleep(.25)
        print('Stop pinging')
        
class InfluxDBWriteThread(Thread):
    def __init__(self):
        Thread.__init__(self)
        
    def run(self):
        while True:
            if not bandwidth_change_queue.empty():
                bandwidth = bandwidth_change_queue.get()
                if bandwidth is not None:
                    #write influx result
                    influxDB2.influx_record_pipe_value_change(influx_utility, bandwidth)
            
            if shutdown_influx_write_event.is_set():
                break
            time.sleep(.25)
        print('Stop looking for writes')
        
def get_pipe_bandwidth():
    r = requests.get(pipe_search_url, timeout=5, auth=auth).json()

    download_pipe_uuid = [x for x in r['rows'] if x['description'] == 'Download'][0]['uuid']

    return requests.get(get_pipe_url + download_pipe_uuid, timeout=5, auth=auth).json()['pipe']['bandwidth']

def get_pipe_info():
    r = requests.get(pipe_search_url, timeout=5, auth=auth).json()

    download_pipe_uuid = [x for x in r['rows'] if x['description'] == 'Download'][0]['uuid']

    return download_pipe_uuid, requests.get(get_pipe_url + download_pipe_uuid, timeout=5, auth=auth).json()
    
def get_current_pipe_loading(last_req_time, last_rx_bytes):
    pipe_setting = float(get_pipe_bandwidth())
    
    rate, last_req_time, last_rx_bytes = get_loading_info(last_req_time, last_rx_bytes)
    time.sleep(0.25)
    rate, last_req_time, last_rx_bytes = get_loading_info(last_req_time, last_rx_bytes)
    
    pipe_loading = rate/pipe_setting
    
    return rate, pipe_loading, last_req_time,last_rx_bytes 
    
def get_loading_info(last_req_time, last_rx_bytes):
    if last_req_time == 0 or last_rx_bytes == 0:
        last_req_time, last_rx_bytes = get_initial_loading_info()
    
    r = requests.get(interfaces_url, timeout=5, auth=auth).json()
    req_time = r['time']
    rx_bytes = float(r['interfaces']['wan']['bytes received'])
        
    elapsed_time = req_time - last_req_time
    new_bytes = (rx_bytes - last_rx_bytes)
    
    return (new_bytes/elapsed_time)/1024/1024*8, req_time, rx_bytes

def get_initial_loading_info():
    r = requests.get(interfaces_url, timeout=5, auth=auth).json()
    req_time = r['time']
    rx_bytes = float(r['interfaces']['wan']['bytes received'])
    time.sleep(1)
    return req_time, rx_bytes

def get_ping_result():
    cloudflare_result = ping('1.1.1.1').rtt_avg_ms
    google_result = ping('1.1.1.1').rtt_avg_ms
    return (cloudflare_result + google_result)/2, datetime.datetime.now().astimezone(to_zone)

def adjust_pipe(percentage):
    # Get the pipe 
    pipe_uuid, pipe = get_pipe_info()
    pipe = pipe['pipe']
    
    # Check to see if resulting bandwidth value will be higher than max value.
    new_bw = round(int(pipe['bandwidth']) * percentage)
    if new_bw > max_pipe_value:
        # New value would be higher than max value.  Set to max value.
        new_bw = max_pipe_value
    elif new_bw < min_pipe_value:
        new_bw = min_pipe_value
    
    # Set bandwidth
    pipe['bandwidth'] = str(new_bw)
    pipe['fqcodel_quantum'] = str(round(new_bw/100*300))
    
    # Resolve enumerators
    pipe['bandwidthMetric'] = get_selected_enumerator(pipe['bandwidthMetric'])
    pipe['mask'] = get_selected_enumerator(pipe['mask'])
    pipe['scheduler'] = get_selected_enumerator(pipe['scheduler'])
    
    # Api doesn't like if we send extraneous info.
    del pipe['number']
    del pipe['origin']
        
    r = requests.post(update_pipe_url + pipe_uuid, json = {"pipe": pipe}, timeout=5, auth=auth)
    
    # Commit the changes
    if r.status_code == 200 and json.loads(r.text)['result'] == 'saved':
        r = requests.post(shaper_reconfigure_url, timeout=5, auth=auth)
        print("Pipe adjusted to " + str(new_bw))
        bandwidth_change_queue.put(new_bw)
    
def get_selected_enumerator(dict):
    for key in dict:
        if dict[key]['selected'] in ['True', 1]:
            return key
            
def main():    
    # Settings
    target_ping = 65
    target_pipe_loading = 0.8
    target_bandwidth_test_loading = 0.8 
       
    rate = 0
    last_req_time = 0
    last_rx_bytes = 0
    pipe_overloads = []   
    bad_pings = []
        
    # Start up the pinger thread
    pinger_thread = PingerThread()
    pinger_thread.start()
    
    # Start up the InfluxDB write thread
    influx_write_thread = InfluxDBWriteThread()
    influx_write_thread.start()
    
    # start_time = time.time()
    # print("Time to get ping form influx: " + str(time.time()-start_time))   
    
    while True:
        if len(pipe_overloads) >= 2:
            # Adjust pipe a little
            print("Pipe needs to be adjusted!")
            adjust_pipe(0.8)
            pipe_overloads.clear()
        if len(bad_pings) >= 1:
            # Aggressively adjust pipe
            print("Pipe needs to be adjusted for bad pings!")
            adjust_pipe(0.25)
            bad_pings.clear()
        
        # current_ping, last_ping_time = influx_get_lastest_ping(query_api)
        #current_ping, last_ping_time = get_ping_result()
        current_ping = pinger_thread.ping['ping']
        last_ping_time = pinger_thread.ping['time']
        
        # First check if we've had a ping within the last 4 seconds
        if (datetime.datetime.now().astimezone(to_zone) - last_ping_time).seconds <= 4:
            # If our ping is past the target ping, we need to investigate further
            if current_ping > target_ping:
                print("Ping is out of tolerance.  current ping: " + str(current_ping))
                
                # Get the current pipe loading to see if we're just using all of the pipe
                rate,pipe_loading, last_req_time, last_rx_bytes = get_current_pipe_loading(last_req_time, last_rx_bytes)

                if pipe_loading > target_pipe_loading:
                    # We are using more than the target loading of the latest download result, we need to decrease pipe size to 80% of the last download result. 
                    # <TODO> Set pipe to 80% of last download result
                    pipe_overloads.append(
                        {
                            'pipe_loading': pipe_loading,
                            'rate': rate,
                            'ping': current_ping
                        }
                    )    
                    print("Using more than the target pipe loading.  Rate: " + str(rate) + " Pipe loading: " + str(pipe_loading))
                else:
                    # Starlink is likely having issues or there are local network issues.  
                    #<TODO> Log message about SL being down
                    bad_pings.append(
                        {
                            'pipe_loading': pipe_loading,
                            'rate': rate,
                            'ping': current_ping
                        }
                    )    
                    
                    print("Bad Pings and Rate. Ping: " + str(current_ping) + ' rate: ' + str(rate) + ' pipe loading: ' + str(pipe_loading))
            else:
                # Our ping is good.  Check pipe loading to see if we can increase the loading
                rate,pipe_loading, last_req_time, last_rx_bytes = get_current_pipe_loading(last_req_time, last_rx_bytes)
                
                # Get the pipe bandwidth setting
                bandwidth = get_pipe_bandwidth()            
                
                if pipe_loading < target_pipe_loading and int(bandwidth) < max_pipe_value:
                    # Loading is low and we're lower than our max pipe bandwidth.  
                    # Increase pipe bandwidth aggressively 
                    adjust_pipe(1.5)
                
                
                # print("All is good! Current ping: " + str(current_ping))
        else:
            # No recent ping.  Skip this loop.  Consider sending email alert.
            print("No ping in the last 4s.  Ping test is not working.")
        time.sleep(0.10)
        # Check if a key has been pressed
        if keyboard.is_pressed("^"):
            shutdown_pinger_event.set()
            shutdown_influx_write_event.set()
            break                
    pinger_thread.join()
    influx_write_thread.join()
    influx_utility.shutdown()

if __name__ == "__main__": 
	main() 
     