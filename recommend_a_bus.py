#!/usr/bin/python

from gtts import gTTS
from sh import mpg123, aplay
import ConfigParser
import arrow
import lxml.etree
import os
import requests
import sys
import tempfile
import alsaaudio
import time
import logging
from multiprocessing.pool import ThreadPool
from evdev import InputDevice, categorize, ecodes, list_devices


if not os.path.exists("nextbuses.ini"):
    open("nextbuses.ini", 'w').write("""
[credentials]
user = TravelineAPIxxx
pass = xxxx

[api]
url = xxxx

[input]
device = Yubico Yubico Yubikey II
key = KEY_ENTER

[audio]
volume = 100
control = PCM
id = 0
intro = 263123__pan14__sine-tri-tone-down-negative-beep-amb-verb.wav

[distance]
# minutes walk from your house
Cliff Street = 6
London Road = 10

[directions]
# directions you're interested in, and the name you want spoken for them (although it's not spoken today)
Bath Town = town
Airport Terminal 5 = airport

[stops]
# stops you're interested in, and the name you want spoken for them
030.... = Cliff Street
035.... = London Road
""")
    print "Wrote out example nextbuses.ini"
    sys.exit(2)
    

def _query_stop(args):
    stop, config, log = args
    buses = []
    request_timestamp = str(arrow.utcnow())

    siri =  lxml.etree.Element("{http://www.siri.org.uk/}Siri")
    siri.attrib['version'] = "1.0"
    sr = lxml.etree.SubElement(siri, "{http://www.siri.org.uk/}ServiceRequest")
    lxml.etree.SubElement(sr, "{http://www.siri.org.uk/}RequestTimestamp").text = request_timestamp
    lxml.etree.SubElement(sr, "{http://www.siri.org.uk/}RequestorRef").text = config.get('credentials','user')
    monitoringrequest = lxml.etree.SubElement(sr, "{http://www.siri.org.uk/}StopMonitoringRequest")
    monitoringrequest.attrib['version'] = "1.0"
    lxml.etree.SubElement(monitoringrequest, "{http://www.siri.org.uk/}RequestTimestamp").text = request_timestamp
    lxml.etree.SubElement(monitoringrequest, "{http://www.siri.org.uk/}MonitoringRef").text = stop
    lxml.etree.SubElement(monitoringrequest, "{http://www.siri.org.uk/}MessageIdentifier").text = stop

    payload = lxml.etree.tostring(siri, encoding="UTF-8", xml_declaration=True, method='xml', pretty_print=True)

    log.debug("Sending query for stop %s", stop)
    response = requests.post(config.get('api','url'),
                            data=payload,
                            headers={'Content-Type': 'application/xml', 'content-encoding': 'gzip'},
                            auth=(config.get('credentials','user'),
                                  config.get('credentials','pass')))
    log.debug("Got reply")
    siri = lxml.etree.fromstring(response.content)

    visits = siri.findall(".//{http://www.siri.org.uk/}MonitoredStopVisit")
    log.debug("Found %d MonitoredStopVisit", len(visits))
    for visit in visits:
        bus = visit.find(".//{http://www.siri.org.uk/}PublishedLineName").text
        direction = visit.find(".//{http://www.siri.org.uk/}DirectionName").text
        try:
            accuracy = "expected"
            arrival = arrow.get(visit.find(".//{http://www.siri.org.uk/}ExpectedDepartureTime").text)
        except AttributeError:
            accuracy = "scheduled"
            arrival = arrow.get(visit.find(".//{http://www.siri.org.uk/}AimedDepartureTime").text)

        if direction.lower() in config.options('directions'):
            buses.append((arrival, dict(arrival=arrival,
                                        bus=bus,
                                        stop=config.get('stops', stop),
                                        direction=direction,
                                        accuracy=accuracy)))
    return buses

def recommended_buses(config, log):
    buses = []
    queue = [(stop, config, log) for stop in config.options('stops')]
    pool = ThreadPool(len(queue))
    for result in pool.map(_query_stop, queue):
        buses.extend(result)
        
    queued = []
    for arrival, detail in sorted(buses):
        if len(queued) >= 2:
            break
        if arrow.utcnow().replace(minutes=int(config.get('distance', detail['stop']))) < arrival:
            queued.append(detail)
    return queued
            
def find_input_device(log):
    devices = [InputDevice(fn) for fn in list_devices()]
    for dev in devices:
        log.info("Found %s", dev)
        if dev.name == config.get('input', 'device'):
            log.info("Using %s", dev)
            return dev

def say_bus_details(config, log):
    try:
        queued = recommended_buses(config, log)
        info = ["Next bus %s." % queued[0]['arrival'].humanize(arrow.utcnow()),
                "It is the %s which is %s %s from %s." % (queued[0]['bus'],
                                                          queued[0]['accuracy'],
                                                          queued[0]['arrival'].humanize(arrow.utcnow()),
                                                          queued[0]['stop']),
                "A later bus is the number %s which is %s %s from %s." % (queued[1]['bus'],
                                                                          queued[1]['accuracy'],
                                                                          queued[1]['arrival'].humanize(arrow.utcnow()),
                                                                          queued[0]['stop'])]

    except Exception, e:
        log.exception("Unhandled error")
        info = ["We had an error. %s"  % e]

    for block in info:
        # want to start talking as soon as we can, not waiting for mp3 of entire sentence
        tts = gTTS(text=block, lang='en')
        fhandle, fname = tempfile.mkstemp(suffix="mp3")
        tts.save(fname)
        mpg123(fname) # todo - threading so we can download later mp3 blocks while we're still talking
        os.unlink(fname)

config = ConfigParser.ConfigParser()
config.read("nextbuses.ini")

logging.basicConfig(level=logging.DEBUG)
log = logging.getLogger()
    
while True:
    dev = find_input_device(log)
    if not dev:
        log.debug("Didn't find input device, sleeping for 10 seconds")
        time.sleep(10)
        continue

    alsaaudio.Mixer(config.get('audio','control'),
                    int(config.get('audio','id'))).setvolume(int(config.get('audio','volume')))
        
    dev.grab()
    for event in dev.read_loop():
        if event.type == ecodes.EV_KEY:
            if event.value == 0:
                if ecodes.keys[event.code] == config.get('input', 'key'):
                    log.info("Woken up")
                    aplay(config.get('audio', 'intro'), _bg=True)
                    log.info("Getting busses")
                    say_bus_details(config, log)
