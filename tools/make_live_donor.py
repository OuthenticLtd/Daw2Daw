"""Build src/templates/live-donor.als from a Set saved by Ableton Live 11.

    python tools/make_live_donor.py [protos.als]

A Live Set is gzipped XML, and Live refuses one whose elements it does not
expect, so the writer (cubaserea/als_write.py) never invents an element: it
copies the matching record out of this donor and fills it in, the way
cpr_build copies records out of a donor .cpr.

Every record comes from one Set that Live 11.3.43 itself saved, so they all
share one schema revision (a record from another 11.3 build carries other
elements). That Set was made in Live: an audio track with an audio clip,
Pro-Q 4 and Utility on it; a MIDI track with a MIDI clip and Pianoteq 9; a
group holding them; one return track. Out of it come

    LiveSet/Tracks     AudioTrack, MidiTrack, GroupTrack - emptied: no
                       devices, automation or sends; the audio track keeps
                       one AudioClip, the MIDI track one MidiClip, both
                       cleared of the test's file and notes
    ConverterProtos    PluginDevice (effect), PluginDevice (instrument),
                       StereoGain (Utility), ReturnTrack, TrackSendHolder -
                       read only by the writer; the donor is never opened

The plug-ins' own state is cleared from the device records; the writer puts
each converted plug-in's state in.
"""
import copy
import gzip
import os
import sys
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
PROTOS = (sys.argv[1] if len(sys.argv) > 1 else
          os.path.join(os.path.dirname(os.path.dirname(HERE)),
                       'test', 'protos', 'protos-saved Project', 'protos-saved.als'))
OUT = os.path.join(os.path.dirname(HERE), 'src', 'templates', 'live-donor.als')
LIVE = os.environ.get('LIVE_DIR') or r'C:\ProgramData\Ableton\Live 11 Suite'
# a factory preset of each of Live's own effects (the device record is what
# is wanted; its settings are overwritten)
STOCK = {
    'compressor': 'Compressor/Generic Compressor.adv',
    'limiter': 'Limiter/Low Latency.adv',
    'gate': 'Gate/Gated Drums.adv',
    'delay': 'Delay/Ambient Spaces/Disharmonics.adv',
    'reverb': 'Reverb/Hall/Concert Hall.adv',
    'eq8': 'EQ Eight/Drums/Cymbal EQ 1.adv',
    'chorus': 'Chorus-Ensemble/Chorus Classic.adv',
    'autofilter': 'Auto Filter/Swirl.adv',
    # the rest of Live's own audio effects, each one's settings rewritten
    # by live_stock (a preset only carries the device's record)
    'glue': 'Glue Compressor/Bass - Low Extender.adv',
    'multiband': 'Multiband Dynamics/A Standard Multiband Comp.adv',
    'eq3': 'EQ Three/Boost HiHats.adv',
    'channeleq': 'Channel EQ/Boom Capture.adv',
    'echo': 'Echo/Ambient Spaces/Diffused Long Cascades.adv',
    'filterdelay': 'Filter Delay/Ambidel.adv',
    'grain': 'Grain Delay/Ascent.adv',
    'phaserflanger': 'Phaser-Flanger/Doubler Ether.adv',
    'autopan': 'Auto Pan/1to4 Note Contenders.adv',
    'saturator': 'Saturator/A Bit Warmer.adv',
    'overdrive': 'Overdrive/Distort.adv',
    'dyntube': 'Dynamic Tube/Broken Tube.adv',
    'redux': 'Redux/Chiptune Filter.adv',
    'erosion': 'Erosion/Hiss.adv',
    'amp': 'Amp/Bass Roundup.adv',
    'cabinet': 'Cabinet/1x12 Cab.adv',
    'pedal': 'Pedal/Bass Guitar Front of Stage.adv',
    'vinyl': 'Vinyl Distortion/Awfull.adv',
    'drumbuss': 'Drum Buss/Bonzo on the Dials.adv',
    'hybridreverb': 'Hybrid Reverb/Drums/Clap Hybrid.adv',
    'shifter': 'Shifter/Autonomous Photon Ray.adv',
    'corpus': 'Corpus/Bright Snare.adv',
    'resonators': 'Resonators/Berlin.adv',
    'vocoder': 'Vocoder/Chromatic.adv',
}

POINTEE_TAGS = ('AutomationTarget', 'ModulationTarget', 'Pointee')


def is_pointee(e):
    t = e.tag
    return (t in POINTEE_TAGS or t.endswith('ModulationTarget')
            or t.startswith('ControllerTargets.'))


def renumber(elem, nxt):
    """Fresh pointee ids from nxt[0] for everything under `elem`; the
    references inside it (PointeeId) follow."""
    remap = {}
    for e in elem.iter():
        if is_pointee(e) and e.get('Id') is not None:
            remap[e.get('Id')] = str(nxt[0])
            e.set('Id', str(nxt[0]))
            nxt[0] += 1
    for e in elem.iter('PointeeId'):
        v = e.get('Value')
        if v in remap:
            e.set('Value', remap[v])


def strip_track(t):
    t.find('DeviceChain/DeviceChain/Devices').clear()
    # the views remember the selected device by its place in the chain;
    # with the devices gone a place past the end of it made Live crash on
    # load (SelectedDevice 3 on the audio track that had held Pro-Q 4 and
    # Utility). Live's own empty tracks hold 0 and 1 there.
    for e in t.iter('SelectedDevice'):
        if int(e.get('Value') or 0) > 1:
            e.set('Value', '1')
    # out of any group: to the master (a track Live saved inside a group
    # says AudioOut/GroupTrack, which crashed Live on a track in none)
    out = t.find('DeviceChain/AudioOutputRouting')
    out.find('Target').set('Value', 'AudioOut/Master')
    out.find('UpperDisplayString').set('Value', 'Master')
    out.find('LowerDisplayString').set('Value', '')
    t.find('AutomationEnvelopes/Envelopes').clear()
    t.find('DeviceChain/Mixer/Sends').clear()
    for lanes in t.iter('AutomationLanes'):
        inner = lanes.find('AutomationLanes')
        if inner is not None:
            for lane in list(inner)[1:]:
                inner.remove(lane)


def clean_device(d, role=None):
    pre = d.find('PluginDesc/Vst3PluginInfo/Preset/Vst3Preset')
    if pre is not None:
        pre.find('ProcessorState').text = ''
        pre.find('ControllerState').text = ''
    sc = d.find('SourceContext')
    if sc is not None:
        for c in list(sc):
            sc.remove(c)
        ET.SubElement(sc, 'Value')
    if role:
        d.set('role', role)


def main():
    root = ET.fromstring(gzip.open(PROTOS).read())
    ls = root.find('LiveSet')
    tracks = ls.find('Tracks')
    all_tracks = list(tracks)

    def first(tag, with_clip=None):
        for t in all_tracks:
            if t.tag != tag:
                continue
            if with_clip and t.find('.//' + with_clip) is None:
                continue
            return t
    audio = first('AudioTrack', 'AudioClip')
    midi = first('MidiTrack', 'MidiClip')
    group = first('GroupTrack')
    ret = first('ReturnTrack')
    protos = ET.Element('ConverterProtos')
    for t in all_tracks:
        for d in t.find('DeviceChain/DeviceChain/Devices'):
            if d.tag == 'PluginDevice' and d.find('PluginDesc/VstPluginInfo') is not None:
                # a VST2 (Pro-C 2's VST2 build): its preset is the plug-in's
                # program - a 28-byte name and the parameter values
                if protos.find("PluginDevice[@role='vst2']") is None:
                    dd = copy.deepcopy(d)
                    sc = dd.find('SourceContext')
                    for c in list(sc):
                        sc.remove(c)
                    ET.SubElement(sc, 'Value')
                    dd.find('PluginDesc/VstPluginInfo/Preset/VstPreset/Buffer').text = ''
                    dd.set('role', 'vst2')
                    protos.append(dd)
                continue
            if d.tag == 'PluginDevice':
                info = d.find('PluginDesc/Vst3PluginInfo')
                role = 'instrument' if info.find('DeviceType').get('Value') == '1' else 'effect'
                if protos.find("PluginDevice[@role='%s']" % role) is None:
                    dd = copy.deepcopy(d)
                    clean_device(dd, role)
                    protos.append(dd)
            elif d.tag == 'StereoGain' and protos.find('StereoGain') is None:
                dd = copy.deepcopy(d)
                clean_device(dd)
                protos.append(dd)
            elif d.tag == 'InstrumentGroupDevice' and protos.find('InstrumentGroupDevice') is None:
                # an Instrument Rack with one chain, emptied: the writer
                # copies the chain once per instrument
                dd = copy.deepcopy(d)
                clean_device(dd)
                for br in dd.find('Branches'):
                    br.find('DeviceChain/MidiToAudioDeviceChain/Devices').clear()
                    for e in br.iter('SelectedDevice'):
                        e.set('Value', '0')
                protos.append(dd)
                inner = d.find('Branches/InstrumentBranch/DeviceChain/MidiToAudioDeviceChain/Devices')
                for x in inner:
                    if x.tag == 'PluginDevice' and protos.find("PluginDevice[@role='instrument']") is None:
                        xx = copy.deepcopy(x)
                        clean_device(xx, 'instrument')
                        protos.append(xx)
    holder = copy.deepcopy(audio.find('DeviceChain/Mixer/Sends')[0])
    for t in all_tracks:
        tracks.remove(t)
    for t in (audio, midi, group):
        strip_track(t)
        t.find('TrackGroupId').set('Value', '-1')
        tracks.append(t)
    # one clip each, nothing of the test left in it
    aev = audio.find('DeviceChain/MainSequencer/Sample/ArrangerAutomation/Events')
    for c in list(aev)[1:]:
        aev.remove(c)
    aclip = aev[0]
    ref = aclip.find('SampleRef/FileRef')
    for tag, val in (('RelativePathType', '0'), ('RelativePath', ''), ('Path', ''),
                     ('OriginalFileSize', '0'), ('OriginalCrc', '0')):
        ref.find(tag).set('Value', val)
    sc = aclip.find('SampleRef/SourceContext')
    if sc is not None:
        sc.clear()
    aclip.find('Envelopes/Envelopes').clear()
    mev = midi.find('DeviceChain/MainSequencer/ClipTimeable/ArrangerAutomation/Events')
    for c in list(mev)[1:]:
        mev.remove(c)
    mclip = mev[0]
    for kt in list(mclip.find('Notes/KeyTracks')):
        mclip.find('Notes/KeyTracks').remove(kt)
    mclip.find('Envelopes/Envelopes').clear()
    for c in (aclip, mclip):
        c.find('Name').set('Value', '')
    strip_track(ret)
    protos.append(ret)
    protos.append(holder)
    # Live's own effects, for the stock effects of REAPER and Cubase
    # (cubaserea/live_stock.py sets every parameter it maps, and puts the
    # ones it does not map to where the device's default leaves them)
    lib = os.path.join(LIVE, 'Resources', 'Core Library', 'Devices', 'Audio Effects')
    for role, rel in STOCK.items():
        f = os.path.join(lib, *rel.split('/'))
        try:
            d = ET.fromstring(gzip.open(f).read())[0]
        except (OSError, IndexError) as e:
            print('no stock prototype for %s (%s)' % (role, e))
            continue
        d = copy.deepcopy(d)
        clean_device(d, role)
        protos.append(d)
    ls.find('SendsPre').clear()
    ls.find('Locators/Locators').clear()
    # the master: no devices, no automation beyond its tempo/time signature
    mt = ls.find('MasterTrack')
    mt.find('DeviceChain/DeviceChain/Devices').clear()
    for env in mt.find('AutomationEnvelopes/Envelopes'):
        evs = env.find('Automation/Events')
        for e in list(evs)[1:]:
            evs.remove(e)
    nxt = [int(ls.find('NextPointeeId').get('Value'))]
    for c in protos:
        renumber(c, nxt)
    root.append(protos)
    ls.find('NextPointeeId').set('Value', str(nxt[0]))
    audio.set('Id', '10')
    midi.set('Id', '11')
    group.set('Id', '12')
    data = b'<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding='utf-8').split(b'?>', 1)[-1].lstrip()
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with gzip.open(OUT, 'wb') as f:
        f.write(data)
    print('%s: %d bytes (%d of XML), schema %s/%s, protos %s'
          % (OUT, os.path.getsize(OUT), len(data), root.get('MinorVersion'),
             root.get('SchemaChangeCount'),
             [(c.tag, c.get('role')) for c in protos]))


if __name__ == '__main__':
    main()
