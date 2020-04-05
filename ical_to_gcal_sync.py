from __future__ import print_function

import datetime
from dateutil.tz import tzutc
import logging
import time
import string
import re
import sys
import pickle

import googleapiclient
import requests
import ics
import arrow

from auth import auth_with_calendar_api
from config import *

import mmslogin

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)
handler = logging.FileHandler(filename=LOGFILE, mode='a')
handler.setFormatter(
    logging.Formatter('%(asctime)s|[%(levelname)s] %(message)s'))
logger.addHandler(handler)


def get_current_events(ical_feed_url):
    """Retrieves data from iCal iCal feed and returns an ics.Calendar object
    containing the parsed data.

    Returns the parsed Calendar object or None if an error occurs.
    """
    resp = requests.get(ical_feed_url)
    if resp.status_code != 200:
        logger.error('> Error retrieving iCal feed!')
        return None

    try:
        cal = ics.Calendar(resp.text)
    except Exception as e:
        logger.error('> Error parsing iCal data ({})'.format(e))
        return None

    return cal


def get_gcal_events(service, gcal_id, from_time, to_time):
    """Retrieves the current set of Google Calendar events from the selected
    user calendar. Only includes upcoming events (those taking place from start
    of the current day.

    Returns a dict containing the event(s) existing in the calendar.
    """

    # The list() method returns a dict containing various metadata along with the actual calendar entries (if any).
    # It is not guaranteed to return all available events in a single call, and so may need called multiple times
    # until it indicates no more events are available, signalled by the absence of "nextPageToken" in the result dict

    logger.debug('Retrieving Google Calendar events')

    # make an initial call, if this returns all events we don't need to do anything else,,,
    time_data = {'timeMin': from_time}
    if to_time:
        time_data['timeMax'] = to_time
    eventsResult = service.events().list(calendarId=gcal_id,
                                         singleEvents=True,
                                         orderBy='startTime',
                                         showDeleted=True,
                                         **time_data).execute()

    events = eventsResult.get('items', [])
    # if nextPageToken is NOT in the dict, this should be everything
    if 'nextPageToken' not in eventsResult:
        logger.info('> Found {:d} upcoming events in Google Calendar (single page)'.format(len(events)))
        return events

    # otherwise keep calling the method, passing back the nextPageToken each time
    while 'nextPageToken' in eventsResult:
        token = eventsResult['nextPageToken']
        eventsResult = service.events().list(calendarId=gcal_id,
                                             timeMin=from_time,
                                             pageToken=token,
                                             singleEvents=True,
                                             orderBy='startTime',
                                             showDeleted=True).execute()
        newevents = eventsResult.get('items', [])
        events.extend(newevents)
        logger.debug('> Found {:d} events on new page, {:d} total'.format(len(newevents), len(events)))

    logger.info('> Found {:d} upcoming events in Google Calendar (multi page)'.format(len(events)))
    return events


def delete_all_events(service):
    for gc in get_gcal_events(
            service=service,
            gcal_id=CALENDAR_ID,
            from_time=arrow.now().replace(
                hour=0, minute=0, second=0, microsecond=0),
            to_time=None):
        try:
            service.events().delete(
                calendarId=CALENDAR_ID, eventId=gc['id']).execute()
            time.sleep(API_SLEEP_TIME)
        except googleapiclient.errors.HttpError:
            pass  # event already marked as deleted


def get_gcal_datetime(arrow_datetime, gcal_timezone, replace_utc=True):
    """
    Will update the passed in datetime object with the proper timezone and
    output it in the format required by Google Calendar.

    Args:
      arrow_datetime: An arrow datatime object.
      gcal_timesone: A tzinfo object of the desired timezone.
      replace_utc: If True will *replace* the UTC timezone on the
        arrow_datetime. This works around an issue were some ical feeds return
        data in a local timezone, but don't include tzinfo on those dates.
    Returns:
      dict: A Google Calendar datetime + timeZone dict to pass to the gcal API.
    """
    if replace_utc and arrow_datetime.tzinfo == tzutc():
        arrow_datetime = arrow_datetime.replace(tzinfo=gcal_timezone)
    arrow_datetime = arrow_datetime.to(gcal_timezone)
    return {u'dateTime': arrow_datetime.format('YYYY-MM-DDTHH:mm:ssZZ'),
            'timeZone': gcal_timezone}


def get_gcal_date(arrow_datetime):
    """
    Convert a datetime object to a google calendar date dict.
    """
    return {u'date': arrow_datetime.format('YYYY-MM-DD')}


def create_id(uid, begintime, endtime, prefix):
    """ Converts ical UUID, begin and endtime to a valid Gcal ID

    Characters allowed in the ID are those used in base32hex encoding, i.e.
    lowercase letters a-v and digits 0-9, see section 3.1.2 in RFC2938

    The length of the ID must be between 5 and 1024 characters
    https://developers.google.com/resources/api-libraries/documentation/calendar/v3/python/latest/calendar_v3.events.html

    Returns:
        ID
    """
    allowed_chars = string.ascii_lowercase[:22] + string.digits
    temp = re.sub('[^{}]'.format(allowed_chars), '', f'{prefix}{uid.lower()}')
    return f'{temp}{begintime.timestamp}{endtime.timestamp}'


def get_and_filter_ical_feed(ical_feed_url, days_to_sync, event_id_prefix):
    # retrieve events from the iCal feed
    logger.info('> Retrieving events from iCal feed')
    print('get ical')
    ical_cal = get_current_events(ical_feed_url=ical_feed_url)

    if ical_cal is None:
        sys.exit(-1)

    # convert iCal event list into a dict indexed by (converted) iCal UID
    ical_events = {}
    for ev in ical_cal.events:
        if not mmslogin.keep_ical_event(ev):
            continue
        # filter out events in the past, don't care about syncing them
        if arrow.get(ev.begin) > today:
            # optionally filter out events >24 hours ahead
            if days_to_sync > 0:
                tdelta = ev.begin - arrow.now()
                if tdelta.days >= days_to_sync:
                    logger.info(u'Filtering out event {} at {} due to ICAL_DAYS_TO_SYNC={}'.format(ev.name, ev.begin, days_to_sync))
                else:
                    ical_events[create_id(
                        ev.uid, ev.begin, ev.end, event_id_prefix)] = ev
            else:
                ical_events[create_id(
                    ev.uid, ev.begin, ev.end, event_id_prefix)] = ev

    logger.debug('> Collected {:d} iCal events'.format(len(ical_events)))
    print('> Collected {:d} iCal events'.format(len(ical_events)))
    return ical_events


def convert_ical_event_to_gcal(ical_event, gcal_tz, event_id_prefix):
    """
    Convert the ical_event to the gcal format.
    """
    gcal_event = {}
    gcal_event['summary'] = ical_event.name
    gcal_event['id'] = create_id(ical_event.uid,
                                 ical_event.begin,
                                 ical_event.end,
                                 event_id_prefix)
    gcal_event['description'] = f'{ical_event.description} (Imported from mycal.py)'
    gcal_event['location'] = ical_event.location

    # check if no time specified in iCal, treat as all day event if so
    # @TODO: i think every event will have an end with this code.
    delta = arrow.get(ical_event.end) - arrow.get(ical_event.begin)
    # TODO multi-day events?
    if delta.days >= 1:
        gcal_event['start'] = get_gcal_date(ical_event.begin)
        logger.info(f'iCal all-day event {ical_event.name} at {ical_event.begin}')
        if ical_event.has_end:
            gcal_event['end'] = get_gcal_date(ical_event.end)
    else:
        gcal_event['start'] = get_gcal_datetime(ical_event.begin, gcal_tz)
        logger.info(u'iCal event {ical_event.name} at {ical_event.begin}')
        if ical_event.has_end:
            gcal_event['end'] = get_gcal_datetime(ical_event.end, gcal_tz)

    return gcal_event


if __name__ == '__main__':
    # setting up Google Calendar API for use
    logger.debug('> Loading credentials')
    service = auth_with_calendar_api()

    # retrieve events from Google Calendar, starting from beginning of current day
    today = arrow.now().replace(hour=0, minute=0, second=0, microsecond=0)
    logger.info('> Retrieving events from Google Calendar')
    print('get gcal')
    if ICAL_DAYS_TO_SYNC > 0:
        end_time = (today +
                    datetime.timedelta(days=ICAL_DAYS_TO_SYNC)).isoformat()
    else:
        end_time = None
    gcal_events = get_gcal_events(service, CALENDAR_ID, today.isoformat(), end_time)

    ical_events = get_and_filter_ical_feed(
        ical_feed_url=ICAL_FEED, days_to_sync=ICAL_DAYS_TO_SYNC,
        event_id_prefix=UID_PREFIX)

    # retrieve the Google Calendar object itself
    gcal_cal = service.calendars().get(calendarId=CALENDAR_ID).execute()
    gcal_tz = gcal_cal['timeZone']

    logger.info('> Processing Google Calendar events...')
    print('processing gcal', len(gcal_events))
    gcal_event_ids = [ev['id'] for ev in gcal_events]

    # first check the set of Google Calendar events against the list of iCal
    # events. Any events in Google Calendar that are no longer in iCal feed
    # get deleted. Any events still present but with changed start/end times
    # get updated.
    for gcal_event in gcal_events:
        eid = gcal_event['id']

        if eid not in ical_events:
            # if a gcal event has been deleted from iCal, also delete it from gcal.
            # Apparently calling delete() only marks an event as "deleted" but doesn't
            # remove it from the calendar, so it will continue to stick around.
            # If you keep seeing messages about events being deleted here, you can
            # try going to the Google Calendar site, opening the options menu for
            # your calendar, selecting "View bin" and then clicking "Empty bin
            # now" to completely delete these events.
            try:
                logger.info(u'> Deleting event "{}" from Google Calendar...'.format(gcal_event.get('summary', '<unnamed event>')))
                print('do delete')
                service.events().delete(calendarId=CALENDAR_ID, eventId=eid).execute()
                time.sleep(API_SLEEP_TIME)
            except googleapiclient.errors.HttpError:
                pass # event already marked as deleted
        else:
            # Get the ical_event and remove it from the list.
            ical_event = ical_events.pop(eid)

            mmslogin.set_ical_description(ical_event)
            event_dict = convert_ical_event_to_gcal(
                ical_event=ical_event, gcal_tz=gcal_tz,
                event_id_prefix=UID_PREFIX)

            # check if the iCal event has a different: start/end time, name,
            # location, or description, and if so sync the changes to the GCal
            # event
            if gcal_event['summary'] != event_dict['summary'] \
               or gcal_event['description'] != event_dict['description'] \
               or gcal_event['location'] != event_dict['location'] \
               or gcal_event['start'] != event_dict['start'] \
               or gcal_event['end'] != event_dict['end']:

                logger.info(f'> Updating event "{eid}" due to change...')
                gcal_event.update(event_dict)

                service.events().update(
                    calendarId=CALENDAR_ID,
                    eventId=eid, body=gcal_event).execute()
                print('did update')
                time.sleep(API_SLEEP_TIME)

    # now add any iCal events not already in the Google Calendar
    logger.info('> Processing iCal events...')
    print('processing ical')
    for ical_event in ical_events.values():
        mmslogin.set_ical_description(ical_event)
        gcal_event = convert_ical_event_to_gcal(
            ical_event=ical_event, gcal_tz=gcal_tz,
            event_id_prefix=UID_PREFIX)

        try:
            time.sleep(API_SLEEP_TIME)
            service.events().insert(
                calendarId=CALENDAR_ID, body=gcal_event).execute()
        except:
            time.sleep(API_SLEEP_TIME)
            service.events().update(
                calendarId=CALENDAR_ID,
                eventId=gcal_event['id'], body=gcal_event).execute()
