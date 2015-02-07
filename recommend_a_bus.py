#!/usr/bin/python

from gtts import gTTS
from sh import mpg123
import ConfigParser
import arrow
import lxml.etree
import os
import requests
import sys
import tempfile
import alsaaudio

if not os.path.exists("nextbuses.ini"):
    open("nextbuses.ini", 'w').write("""
[credentials]
user = TravelineAPIxxx
pass = xxxx

[api]
url = xxxx

[audio]
volume = 100
control = PCM
id = 0

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
    

def recommended_buses(config):
    buses = []
    request_timestamp = str(arrow.utcnow())

    for stop in config.options('stops'):
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

        response = requests.post(config.get('api','url'),
                                data=payload,
                                headers={'Content-Type': 'application/xml'},
                                auth=(config.get('credentials','user'),
                                      config.get('credentials','pass')))
        siri = lxml.etree.fromstring(response.content)

        for visit in siri.findall(".//{http://www.siri.org.uk/}MonitoredStopVisit"):
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

    queued = []
    for arrival, detail in sorted(buses):
        if len(queued) >= 2:
            break
        if arrow.utcnow().replace(minutes=int(config.get('distance', detail['stop']))) < arrival:
            queued.append(detail)
    return queued
            
config = ConfigParser.ConfigParser()
config.read("nextbuses.ini")

try:
    queued = recommended_buses(config)
    info = "Next bus %s. It is the %s which is %s %s from %s. The one after is the number %s which is %s %s from %s." % (
        queued[0]['arrival'].humanize(arrow.utcnow()),
        queued[0]['bus'], queued[0]['accuracy'], queued[0]['arrival'].humanize(arrow.utcnow()), queued[0]['stop'],
        queued[1]['bus'], queued[1]['accuracy'], queued[1]['arrival'].humanize(arrow.utcnow()), queued[1]['stop'])
except Exception, e:
    import traceback
    print e
    traceback.print_exc()
    info = "We had an error. %s"  % e
        
tts = gTTS(text=info, lang='en')
fhandle, fname = tempfile.mkstemp(suffix="mp3")
tts.save(fname)
alsaaudio.Mixer(config.get('audio','control'),
                int(config.get('audio','id'))).setvolume(int(config.get('audio','volume')))
mpg123(fname)
os.unlink(fname)

