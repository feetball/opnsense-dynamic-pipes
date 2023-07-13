from dateutil import tz
from functools import reduce
from influxdb_client import InfluxDBClient, Point

def influx_get_latest_download_result(influx_utility):
    # run the query
    download_query = influx_utility.query_api.query(org=influx_utility.org, query=influx_utility.speedtest_query)
    last_download_result = download_query[0].records[-1].get_value()
    last_download_time = download_query[0].records[-1].get_time().astimezone(influx_utility.to_zone)

    return last_download_result, last_download_time

def influx_get_latest_ping(influx_utility):
    results = []
       
    # run the query
    ping_query_result = influx_utility.query_api.query(org=influx_utility.org, query=influx_utility.ping_query)
        
    results.append(ping_query_result[0].records[0].values['_value'])
    results.append(ping_query_result[1].records[0].values['_value'])
    ping_time = ping_query_result[1].records[0].values['_time']
        
    result_mean = reduce(lambda a, b: a + b, results) / len(results)    
    return result_mean, ping_time

def influx_record_pipe_value_change(influx_utility, pipe_value):
    influx_utility.write_api.write(influx_utility.pipe_change_bucket, influx_utility.org, Point("pipe_changes").tag("pipe","Download").field("bandwidth", pipe_value))

class InfluxDB2Utility:
    """ A class for reading and writing to/from InfluxDB2 """
    def __init__(self):
        self.influx_url = "http://influx2:8086"
        self.org = "influx_org"
        self.token = 'influx_token'
        self.pipe_change_bucket = 'influx_bucket'
        # create a connection to influxdb
        self.client = InfluxDBClient(url=self.influx_url, token=self.token, org=self.org)
        self.to_zone = tz.tzlocal()
        # instantiate the QueryAPI
        self.query_api = self.client.query_api()
        self.write_api = self.client.write_api()
        
        #queries
        self.ping_query  = '''from(bucket: "SpeedFlux")
            |> range(start: 0)
            |> filter(fn: (r) => r["_measurement"] == "pings")
            |> filter(fn: (r) => r["_field"] == "rtt")
            |> tail(n: 1)'''
            
        speedtest_query = '''from(bucket: "SpeedFlux")"
            |> range(start: 0)
            |> filter(fn: (r) => r["_measurement"] == "speeds")
            |> filter(fn: (r) => r["_field"] == "bandwidth_down")
            |> tail(n: 1)'''
            
    def shutdown(self):
        self.client.close()