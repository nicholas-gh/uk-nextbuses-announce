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
from threading import Thread
from Queue import Queue
from optparse import OptionParser
from evdev import InputDevice, categorize, ecodes, list_devices

parser = OptionParser()
parser.add_option("-c", "--config", dest="configfilename",
                  default="nextbuses.ini",
                  help="Reading configuration from FILE", metavar="FILE")

parser.add_option("-d", "--directions", dest="directions",
                  default=False,
                  action="store_true",
                  help="Print discovered directions")

(options, args) = parser.parse_args()

if not os.path.exists(options.configfilename):
    open(options.configfilename, 'w').write("""
[credentials]
user = TravelineAPIxxx
pass = xxxx

[api]
url = xxxx

[input]
device = Yubico Yubico Yubikey II
key = KEY_ENTER
sleep = 10
announce_on_connect = true

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
    stop, config, log, options = args
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

        if options.directions or direction.lower() in config.options('directions'):
            buses.append((arrival, dict(arrival=arrival,
                                        bus=bus,
                                        stop=config.get('stops', stop),
                                        direction=direction,
                                        accuracy=accuracy)))
            
    return buses

def recommended_buses(config, log, options):
    queued = []
    for arrival, detail in sorted(all_buses(config, log, options)):
        if len(queued) >= 2:
            break
        if arrow.utcnow().replace(minutes=config.getint('distance', detail['stop'])) < arrival:
            queued.append(detail)
    return queued

def all_buses(config, log, options):
    buses = []
    queue = [(stop, config, log, options) for stop in config.options('stops')]
    pool = ThreadPool(len(queue))
    for result in pool.map(_query_stop, queue):
        buses.extend(result)
    return buses

def bus_directions(config, log, options):
    directions = set()
    for arrival, detail in sorted(all_buses(config, log, options)):
        directions.add(detail['direction'])
    for direction in directions:
        yield direction

def find_input_device(log):
    devices = [InputDevice(fn) for fn in list_devices()]
    for dev in devices:
        log.info("Found %s", dev)
        if dev.name == config.get('input', 'device'):
            log.info("Using %s", dev)
            return dev

sound_queue = Queue()

def say_bus_details(config, log, options):
    global sound_queue
    
    try:
        queued = recommended_buses(config, log, options)
        if queued is None:
            return
        for detail in queued:
            detail['due'] = detail['arrival'].humanize(arrow.utcnow())
        info = ["Next bus %s, and then %s" % (queued[0]['due'], queued[1]['due']),
                "It is number %s, %s %s from %s." % (queued[0]['bus'],
                                                     queued[0]['accuracy'],
                                                     queued[0]['due'],
                                                     queued[0]['stop'])]
        if (queued[0]['bus'], queued[0]['stop']) == (queued[1]['bus'], queued[1]['stop']):
            info.append("and then %s %s." % (queued[1]['accuracy'],
                                             queued[1]['due']))
        else:
            info.append("A later bus is the number %s, %s %s from %s." % (queued[1]['bus'],
                                                                          queued[1]['accuracy'],
                                                                          queued[1]['due'],
                                                                          queued[1]['stop']))

    except Exception, e:
        log.exception("Unhandled error")
        info = ["We had an error. %s"  % e]

    def sound_worker(queue):
        while True:
            fname = queue.get(block=True)
            if fname is None:
                return
            mpg123(fname)
            os.unlink(fname)

    worker = Thread(target=sound_worker, args=(sound_queue,))
    worker.start()
        
    for block in info:
        # want to start talking as soon as we can, not waiting for mp3 of entire sentence
        tts = gTTS(text=block, lang='en')
        fhandle, fname = tempfile.mkstemp(suffix="mp3")
        tts.save(fname)
        sound_queue.put(fname)
    sound_queue.put(None)

config = ConfigParser.ConfigParser()
config.read(options.configfilename)

logging.basicConfig(level=logging.DEBUG)
log = logging.getLogger()

if options.directions:
    print "Discovered bus directions from configured stops:"
    for direction in bus_directions(config, log, options):
        print direction
    sys.exit(2)

first_search = True
while True:
    dev = find_input_device(log)
    if not dev:
        sleep = config.getint('input', 'sleep')
        log.debug("Didn't find input device, sleeping for %d seconds", sleep)
        time.sleep(sleep)
        first_search = False
        continue
    if first_search == False and config.getboolean('input', 'announce_on_connect'):
        aplay(config.get('audio', 'intro'), _bg=True)
        log.info("Getting buses")
        say_bus_details(config, log, options)

    alsaaudio.Mixer(config.get('audio','control'),
                    config.getint('audio','id')).setvolume(config.getint('audio','volume'))
        
    dev.grab()
    try:
        for event in dev.read_loop():
            if event.type == ecodes.EV_KEY:
                if event.value == 0:
                    if ecodes.keys[event.code] == config.get('input', 'key'):
                        if sound_queue.qsize() > 0:
                            # don't talk over ourselves
                            log.debug("Still talking...")
                            continue
                        log.info("Woken up")
                        aplay(config.get('audio', 'intro'), _bg=True)
                        log.info("Getting buses")
                        say_bus_details(config, log, options)
    except IOError, e:
        if e.errno == 19:
            logging.debug("input device has gone.")
            continue
