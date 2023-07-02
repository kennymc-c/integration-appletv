import asyncio
import base64
import logging
import random
import json
import os

import ucapi.api as uc
import ucapi.entities as entities

import pyatv
import pyatv.const

from pyatv.interface import PushListener

LOG = logging.getLogger(__name__)
LOOP = asyncio.get_event_loop()
LOG.setLevel(logging.DEBUG)

# Global variables
dataPath = None
api = uc.IntegrationAPI(LOOP)
credentials = {
    'identifier': "",
    'credentials': []
}
pairingAtv = None
pairingProcess = None
connectedAtv = None
isConnected = False
pollingTask = None

async def retry(fn, retries=5):
    i = 0
    while True:
        try:
            return await fn()
        except:
            if i == retries:
                LOG.debug('Retry limit reached for %s', fn)
                raise
            await asyncio.sleep(2)
            i += 1

async def commandWrapper(fn):
    try:
        await fn()
        return uc.uc.STATUS_CODES.OK
    except:
        return uc.uc.STATUS_CODES.SERVER_ERROR
    
async def clearCredentials():
    global credentials

    credentials = {
        'identifier': "",
        'credentials': []
    }

    if os.path.exists(dataPath + '/credentials.json'):
        os.remove(dataPath + '/credentials.json')

async def storeCredentials():
    f = None
    try:
        f= open(dataPath + '/credentials.json', 'w+')
    except OSError:
        LOG.error('Cannot write the credentials file')
        return

    json.dump(credentials, f, ensure_ascii=False)

    f.close()

async def loadCredentials():
    global credentials

    f = None

    try:
        f = open(dataPath + '/credentials.json', 'r')
    except OSError:
        LOG.error('Cannot open the credentials file')
    
    if f is None:
        return False

    try:
        data = json.load(f)
        f.close()
    except ValueError:
        LOG.error('Empty credentials file')
        return False

    credentials['identifier'] = data['identifier']
    credentials['credentials'] = data['credentials']

    return True
        
async def findAppleTv(identifier):
    atvs = await pyatv.scan(LOOP, identifier=identifier)
    if not atvs:
        return None
    else:
        return atvs[0]

async def discoverAppleTVs():
    atvs = await pyatv.scan(LOOP)
    res = []

    for tv in atvs:
        # We only support TvOS
        if tv.device_info.operating_system == pyatv.const.OperatingSystem.TvOS:
            res.append(tv)

    return res

async def disconnectFromAppleTv():
    global connectedAtv
    global isConnected
    if connectedAtv is not None:
        connectedAtv.close()
        connectedAtv = None

    isConnected = False
    LOG.debug('Disconnected')

async def connectToAppleTv():
    global connectedAtv
    global isConnected
    global credentials
    tv = None

    if isConnected == True:
        return False

    if credentials['identifier'] == "":
        return False

    if connectedAtv is None:
        tv = await findAppleTv(credentials['identifier'])

        if tv is None:
            LOG.error('Cannot find AppleTV to connect to')
            raise
            # return False
    
    for credential in credentials['credentials']:
        protocol = None
        if credential['protocol'] == 'companion':
            protocol = pyatv.const.Protocol.Companion
        elif credential['protocol'] == 'airplay':
            protocol = pyatv.const.Protocol.AirPlay

        res = tv.set_credentials(protocol, credential['credentials'])
        if res is False:
            LOG.error('Failed to set credentials')
            raise
        else:
            LOG.debug('Credentials set for %s', protocol)
        # return False

    LOG.debug('Connecting to %s', tv)

    connectedAtv = await pyatv.connect(tv, LOOP)

    entity = entities.media_player.MediaPlayer(tv.identifier, tv.name, [
            entities.media_player.FEATURES.ON_OFF,
            # entities.media_player.FEATURES.VOLUME,
            entities.media_player.FEATURES.VOLUME_UP_DOWN,
            # entities.media_player.FEATURES.MUTE_TOGGLE,
            entities.media_player.FEATURES.PLAY_PAUSE,
            entities.media_player.FEATURES.NEXT,
            entities.media_player.FEATURES.PREVIOUS,
            entities.media_player.FEATURES.MEDIA_DURATION,
            entities.media_player.FEATURES.MEDIA_POSITION,
            entities.media_player.FEATURES.MEDIA_TITLE,
            entities.media_player.FEATURES.MEDIA_ARTIST,
            entities.media_player.FEATURES.MEDIA_ALBUM,
            entities.media_player.FEATURES.MEDIA_IMAGE_URL                                       
        ], {
            entities.media_player.ATTRIBUTES.STATE: entities.media_player.STATES.OFF,
            # entities.media_player.ATTRIBUTES.VOLUME: 0,
            # entities.media_player.ATTRIBUTES.MUTED: False,
            entities.media_player.ATTRIBUTES.MEDIA_DURATION: 0,
			entities.media_player.ATTRIBUTES.MEDIA_POSITION: 0,
			entities.media_player.ATTRIBUTES.MEDIA_IMAGE_URL: "",
			entities.media_player.ATTRIBUTES.MEDIA_TITLE: "",
			entities.media_player.ATTRIBUTES.MEDIA_ARTIST: "",
			entities.media_player.ATTRIBUTES.MEDIA_ALBUM: ""
        })
    api.availableEntities.addEntity(entity)

    isConnected = True

    LOG.debug('Connected')

    return True

async def finishPairing():
    global pairingProcess
    res = None

    await pairingProcess.finish()

    if pairingProcess.has_paired:
        LOG.debug("Paired with device!")
        res = pairingProcess.service
    else:
        LOG.warning('Did not pair with device!')

    await pairingProcess.close()
    pairingProcess = None

    return res

async def polling():
    global api
    global connectedAtv
    global isConnected
    prevHash = None
    while True:
        if isConnected is False:
            prevHash = None

        if api.configuredEntities.contains(connectedAtv.service.identifier):
            playing = await connectedAtv.metadata.playing()
            power = connectedAtv.power
            # audio = connectedAtv.audio

            # LOG.debug('Volume: %d', audio.volume)

            state = entities.media_player.STATES.UNKNOWN

            if power.power_state is pyatv.const.PowerState.On:
                state = entities.media_player.STATES.ON

                if playing.device_state == pyatv.const.DeviceState.Playing:
                    state = entities.media_player.STATES.PLAYING
                elif playing.device_state == pyatv.const.DeviceState.Paused:
                    state = entities.media_player.STATES.PAUSED
                elif playing.device_state == pyatv.const.DeviceState.Idle:
                    state = entities.media_player.STATES.PAUSED

            elif power.power_state is pyatv.const.PowerState.Off:
                state = entities.media_player.STATES.OFF

            attributes = {
                entities.media_player.ATTRIBUTES.STATE: state,
                entities.media_player.ATTRIBUTES.MEDIA_POSITION: playing.position,
            }
            
            # Update if content changed
            if playing.hash != prevHash:
                try:
                    artwork = await connectedAtv.metadata.artwork(width=480, height=None)
                    artwork_encoded = 'data:image/png;base64,' + base64.b64encode(artwork.bytes).decode('utf-8')
                    attributes[entities.media_player.ATTRIBUTES.MEDIA_IMAGE_URL] = artwork_encoded
                except:
                    LOG.error('OMG')
                
                attributes[entities.media_player.ATTRIBUTES.MEDIA_DURATION] = playing.total_time
                attributes[entities.media_player.ATTRIBUTES.MEDIA_TITLE] = playing.title
                attributes[entities.media_player.ATTRIBUTES.MEDIA_ARTIST] = playing.artist
                attributes[entities.media_player.ATTRIBUTES.MEDIA_ALBUM] = playing.album

            prevHash = playing.hash

            api.configuredEntities.updateEntityAttributes(
                    connectedAtv.service.identifier,
                    attributes
                )

        await asyncio.sleep(2)

def startPolling():
    global pollingTask
    global connectedAtv

    if connectedAtv is None:
        return

    if pollingTask is not None:
        return

    pollingTask = LOOP.create_task(polling())
    LOG.debug('Polling started')

def stopPolling():
    global pollingTask
    if pollingTask is not None:
        pollingTask.cancel()
        pollingTask = None
        LOG.debug('Polling stopped')

# DRIVER SETUP
@api.events.on(uc.uc.EVENTS.SETUP_DRIVER)
async def event_handler(websocket, id, data):
    LOG.debug('Starting driver setup')
    await clearCredentials()
    await api.acknowledgeCommand(websocket, id)
    await api.driverSetupProgress(websocket)

    LOG.debug('Starting Apple TV discovery')
    tvs = await discoverAppleTVs();
    dropdownItems = []

    for tv in tvs:
        tvData = {
            'id': tv.identifier,
            'label': {
                'en': tv.name + " TvOS " + str(tv.device_info.version)
            }
        }

        dropdownItems.append(tvData)

    if not dropdownItems:
        LOG.warning('No Apple TVs found')
        await api.driverSetupError(websocket, 'No Apple TVs found')
        return

    await api.requestDriverSetupUserInput(websocket, 'Please choose your Apple TV', [
        { 
        'field': { 
            'dropdown': {
                'value': dropdownItems[0]['id'],
                'items': dropdownItems
            }
        },
        'id': 'choice',
        'label': { 'en': 'Choose your Apple TV' }
        }
    ])

@api.events.on(uc.uc.EVENTS.SETUP_DRIVER_USER_DATA)
async def event_handler(websocket, id, data):
    await api.acknowledgeCommand(websocket, id)
    await api.driverSetupProgress(websocket)

    global pairingProcess
    global pairingAtv

    # TODO add timeout for inputs

    # We pair with companion second
    if "pin_companion" in data:
        LOG.debug('User has entered the Companion PIN')
        pairingProcess.pin(data['pin_companion'])
        res = await finishPairing()
        if res is None:
            await api.driverSetupError(websocket, 'Unable to pair with Apple TV')
        else:
            c = {
                'protocol': res.protocol.name.lower(),
                'credentials': res.credentials
            }
            credentials['credentials'].append(c)
            await storeCredentials()

            await retry(connectToAppleTv)
            await api.driverSetupComplete(websocket)
    
    # We pair with airplay first
    elif "pin_airplay" in data:
        LOG.debug('User has entered the Airplay PIN')
        pairingProcess.pin(data['pin_airplay'])
        res = await finishPairing()
        if res is None:
            await api.driverSetupError(websocket, 'Unable to pair with Apple TV')
        else:
            # Store credentials
            credentials['identifier'] = pairingAtv.identifier
            c = {
                'protocol': res.protocol.name.lower(),
                'credentials': res.credentials
            }
            credentials['credentials'].append(c)
            await storeCredentials()

            #ask for new pin
            pairingProcess = await pyatv.pair(pairingAtv, pyatv.const.Protocol.Companion, LOOP, name="Remote Two Companion")
            await pairingProcess.begin()

            if pairingProcess.device_provides_pin:
                LOG.debug('Device provides PIN')
                await api.requestDriverSetupUserInput(websocket, 'Please enter the PIN from your Apple TV', [
                    { 
                    'field': { 
                        'number': { 'max': 9999, 'min': 0, 'value': 0000 }
                    },
                    'id': 'pin_companion',
                    'label': { 'en': 'Apple TV PIN' }
                    }
                ])
            else:
                LOG.debug('We provide PIN')
                pin = random.randint(1000,9999)
                pairingProcess.pin(pin)
                await api.requestDriverSetupUserConfirmation(websocket, 'Please enter the following PIN on your Apple TV:' + pin)
                await finishPairing()

    elif "choice" in data:
        choice = data['choice']
        LOG.debug('Chosen Apple TV: ' + choice)
        
        atvs = await pyatv.scan(LOOP, identifier=choice)

        if not atvs:
            LOG.error('Cannot find the chosen AppleTV')
            await api.driverSetupError(websocket, 'There was an error during the setup process')
            return

        LOG.debug('Pairing process begin')
        pairingAtv = atvs[0]
        pairingProcess = await pyatv.pair(pairingAtv, pyatv.const.Protocol.AirPlay, LOOP, name="Remote Two Airplay")
        await pairingProcess.begin()

        if pairingProcess.device_provides_pin:
            LOG.debug('Device provides PIN')
            await api.requestDriverSetupUserInput(websocket, 'Please enter the PIN from your Apple TV', [
                { 
                'field': { 
                    'number': { 'max': 9999, 'min': 0, 'value': 0000 }
                },
                'id': 'pin_airplay',
                'label': { 'en': 'Apple TV PIN' }
                }
            ])
        else:
            LOG.debug('We provide PIN')
            pin = random.randint(1000,9999)
            pairingProcess.pin(pin)
            await api.requestDriverSetupUserConfirmation(websocket, 'Please enter the following PIN on your Apple TV:' + pin)
            await finishPairing(websocket)

    else:
        LOG.error('No choice was received')
        await api.driverSetupError(websocket, 'No Apple TV was selected')

@api.events.on(uc.uc.EVENTS.CONNECT)
async def event_handler():
    global isConnected

    if isConnected is False:
        res = await retry(connectToAppleTv)
        if res == True:
            await api.setDeviceState(uc.uc.DEVICE_STATES.CONNECTED)
        else:
            await api.setDeviceState(uc.uc.DEVICE_STATES.DISCONNECTED)
    else:
        startPolling()
        await api.setDeviceState(uc.uc.DEVICE_STATES.CONNECTED)

@api.events.on(uc.uc.EVENTS.DISCONNECT)
async def event_handler():
    stopPolling()
    await disconnectFromAppleTv()
    await api.setDeviceState(uc.uc.DEVICE_STATES.DISCONNECTED)

@api.events.on(uc.uc.EVENTS.ENTER_STANDBY)
async def event_handler():
    stopPolling()
    await disconnectFromAppleTv()

@api.events.on(uc.uc.EVENTS.EXIT_STANDBY)
async def event_handler():
    res = await retry(connectToAppleTv)
    if res == True:
        startPolling()

@api.events.on(uc.uc.EVENTS.SUBSCRIBE_ENTITIES)
async def event_handler(entityIds):
    global connectedAtv

    if connectedAtv is None:
        await api.setDeviceState(uc.uc.DEVICE_STATES.ERROR)
        return

    startPolling()
    # We only have one appleTv per driver for now
    # for entityId in entityIds:
    #     if entityId == connectedAtv.service.identifier:
    #         LOG.debug('We have a match, start listening to events')

@api.events.on(uc.uc.EVENTS.UNSUBSCRIBE_ENTITIES)
async def event_handler(entityIds):
    global connectedAtv

    if connectedAtv is None:
        await api.setDeviceState(uc.uc.DEVICE_STATES.ERROR)
        return

    # We only have one appleTv per driver for now
    # for entityId in entityIds:
    #     if entityId == connectedAtv.service.identifier:
    #         LOG.debug('We have a match, stop listening to events')

#TODO handle commands
@api.events.on(uc.uc.EVENTS.ENTITY_COMMAND)
async def event_handler(websocket, id, entityId, entityType, cmdId, params):
    global connectedAtv

    rc = connectedAtv.remote_control
    power = connectedAtv.power
    audio = connectedAtv.audio

    if cmdId == entities.media_player.COMMANDS.PLAY_PAUSE:
        res = await commandWrapper(rc.play_pause)
        await api.acknowledgeCommand(websocket, id, res)
    elif cmdId == entities.media_player.COMMANDS.NEXT:
        res = await commandWrapper(rc.next)
        await api.acknowledgeCommand(websocket, id, res)
    elif cmdId == entities.media_player.COMMANDS.PREVIOUS:
        res = await commandWrapper(rc.previous)
        await api.acknowledgeCommand(websocket, id, res)
    elif cmdId == entities.media_player.COMMANDS.VOLUME_UP:
        res = await commandWrapper(audio.volume_up)
        await api.acknowledgeCommand(websocket, id, res)
    elif cmdId == entities.media_player.COMMANDS.VOLUME_DOWN:
        res = await commandWrapper(audio.volume_down)
        await api.acknowledgeCommand(websocket, id, res)
    elif cmdId == entities.media_player.COMMANDS.ON:
        res = await commandWrapper(power.turn_on)
        await api.acknowledgeCommand(websocket, id, res)
    elif cmdId == entities.media_player.COMMANDS.OFF:
        res = await commandWrapper(power.turn_off)
        await api.acknowledgeCommand(websocket, id, res)

async def main():
    global dataPath

    await api.init('driver.json')
    dataPath = api.configDirPath

    res = await loadCredentials()
    if res is True:
        try:
            await connectToAppleTv()
        except:
            LOG.error('Cannot connect')

if __name__ == "__main__":
    LOOP.run_until_complete(main())
    LOOP.run_forever()