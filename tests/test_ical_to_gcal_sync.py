from dateutil.tz import tzutc
import os

import arrow
import ics
from googleapiclient.discovery import build
from googleapiclient.http import HttpMockSequence
import pytest

from ical_to_gcal_sync import *
import config

from tests.mock_ics_feed_response import MOCK_ICS_FEED_RESPONSE
from tests import mock_gcal_responses


def test_get_current_events(requests_mock):
    """
    Verify that we can get and parse ical events.
    """
    ical_feed_url = 'https://testapp.com/icalfeed'

    # Empty calendar response.
    requests_mock.get(ical_feed_url, text='')
    cal = get_current_events(ical_feed_url)
    assert cal is None

    # Calendar with 10 valid events.
    requests_mock.get(ical_feed_url, text=MOCK_ICS_FEED_RESPONSE)
    cal = get_current_events(ical_feed_url)
    assert len(cal.events) == 10


def test_get_gcal_events(requests_mock):
    """
    Make sure that our fetching of gcal events works.
    """
    f = open(os.path.join(os.path.dirname(__file__),
                          'gcal_service_discovery.json'), 'rb')
    discovery_data = f.read()
    f.close()

    http = HttpMockSequence([
        ({'status': '200'}, discovery_data),
        ({'status': '200'}, mock_gcal_responses.gcal_page_2),
        ({'status': '200'}, mock_gcal_responses.gcal_page_1),
        ({'status': '200'}, mock_gcal_responses.gcal_page_2)])

    # Auth seems to be not needed when mocked.
    service = build('calendar', 'v3', http=http)

    # The query params can't really be tested with Google's HttpMock,
    # so we will just check the pagination logic.

    # This first fetch will get 1 page with 2 events.
    gcal_data = get_gcal_events(service, '42', arrow.now(), None)
    assert len(gcal_data) == 2

    # This fetch will get 2 pages with a total of 7 events.
    gcal_data = get_gcal_events(service, '42', arrow.now(), None)
    assert len(gcal_data) == 7


@pytest.mark.skip(reason="no way of currently testing this")
def test_delete_all_events():
    """
    Since this just just a series of calls to the calendar service, we can't
    easily validate it.
    """
    pass


def test_get_gcal_datetime():
    """
    Verify that we can convert from the ical datetime to the gcal format with
    timezone.
    """
    test_tz = 'America/Los_Angeles'
    test_date = arrow.get(2020, 3, 4, 10, 15, 30)
    assert test_date.tzinfo == tzutc()

    res = get_gcal_datetime(test_date, test_tz)
    # The time should be the same, but it had the new timezone added
    assert res['dateTime'].startswith(test_date.format('YYYY-MM-DDTHH:mm:ss'))
    assert res['timeZone'] == test_tz

    res = get_gcal_datetime(test_date, test_tz, False)
    # The time should not be the same because of timezone adjust.
    assert res['dateTime'].startswith(test_date.format('YYYY-MM-DDT02:mm:ss'))
    assert res['timeZone'] == test_tz

    res1 = get_gcal_datetime(test_date.replace(tzinfo='America/New_York'),
                             test_tz)
    res2 = get_gcal_datetime(test_date.replace(tzinfo='America/New_York'),
                             test_tz, False)
    # Since there is a non-UTC timzone provided, it should not be replaced
    # and both results should be the same.
    assert res1['dateTime'].startswith(test_date.format('YYYY-MM-DDT07:mm:ss'))
    assert res1['timeZone'] == test_tz
    assert res1 == res2


def test_get_gcal_date():
    """
    Check that the gcal date if formatted correctly.
    """
    test_tz = 'America/Los_Angeles'
    test_date = arrow.get(2020, 3, 4, 4, 15, 30)
    assert test_date.tzinfo == tzutc()

    # No matter the timezone, it should be the same date.
    res1 = get_gcal_date(test_date)
    res2 = get_gcal_date(test_date.replace(tzinfo=test_tz))

    assert res1['date'] == test_date.format('YYYY-MM-DD')
    assert res1 == res2


def test_create_id():
    """
    Check our algorithm for creating unique gcal IDs.
    """
    begin_date = arrow.get(2020, 3, 4, 4, 15, 30)
    end_date = arrow.get(2020, 3, 4, 10, 15, 30)

    suffix = f'{begin_date.timestamp}{end_date.timestamp}'

    # No prefix.
    uid = create_id('foo', begin_date, end_date, '')
    assert uid.startswith('foo')
    assert uid.endswith(suffix)

    # Invalid prefix.
    uid = create_id('foo', begin_date, end_date, '&')
    assert uid.startswith('foo')
    assert uid.endswith(suffix)

    # Invalid chars in id.
    uid = create_id('f#o&o!!!', begin_date, end_date, '&')
    assert uid.startswith('foo')
    assert uid.endswith(suffix)

    # Valid prefix.
    uid = create_id('foo', begin_date, end_date, 'bar')
    assert uid.startswith('barfoo')
    assert uid.endswith(suffix)


def test_get_and_filter_ical_feed(requests_mock):
    """
    Validate the filtering mechanism of the ical feed.
    """
    ical_feed_url = 'https://testapp.com/icalfeed'
    # Calendar with 10 valid events.
    requests_mock.get(ical_feed_url, text=MOCK_ICS_FEED_RESPONSE)

    resp = get_and_filter_ical_feed(ical_feed_url, 0, 'bob')
    assert len(resp) == 10

    # The events in our mock file are from June and July of the year 2222.
    # If this code is still running in 2222, I will be 242 years young!
    day_count = (arrow.get(2222, 7, 1) - arrow.now()).days
    resp = get_and_filter_ical_feed(ical_feed_url, day_count, 'bob')
    assert len(resp) == 2

    def filter_func(ical_event):
        # A simple filter for testing.
        eint = 0
        try:
            eint = int(ical_event.name[-1])
        except:
            pass
        return (eint % 2) == 0

    resp = get_and_filter_ical_feed(ical_feed_url, 0, 'bob', filter_func)
    assert len(resp) == 5


def test_convert_ical_event_to_gcal():
    """
    Test the conversion from ical event format to the gcal event dict.
    """
    begin_date = arrow.get(2020, 3, 4, 4, 15, 30)
    end_date = arrow.get(2020, 3, 4, 10, 15, 30)
    gcal_tz = 'America/Los_Angeles'

    ical_event = ics.event.Event(name='Test Event 1',
                                 begin=begin_date,
                                 end=end_date,
                                 uid='42',
                                 description='Test Descriptions for Event 1',
                                 location='Conference Room 1')

    resp = convert_ical_event_to_gcal(ical_event, gcal_tz, 'bob')
    assert resp['summary'] == ical_event.name
    assert resp['id'].startswith('bob42')
    assert resp['description'].startswith(ical_event.description)
    assert resp['location'] == ical_event.location
    # The conversion of the start and end times was tested above.
    assert resp['start'] is not None
    assert resp['end'] is not None


def test_delete_or_update_gcal_events(caplog):
    """
    Test that given a set of gcal and ical events that the proper ones are
    deleted or updated.
    """
    f = open(os.path.join(os.path.dirname(__file__),
                          'gcal_service_discovery.json'), 'rb')
    discovery_data = f.read()
    f.close()

    http = HttpMockSequence([
        ({'status': '200'}, discovery_data),
        ({'status': '200'}, 'echo_request_uri'),  # Delete Call
        ({'status': '200'}, 'echo_request_body'),  # Update Call
    ])

    # Auth seems to be not needed when mocked.
    service = build('calendar', 'v3', http=http)
    assert len(http._iterable) == 2

    gcal_events = []
    # Create 4 gcal events:
    # Non-matching ID prefix
    gcal_events.append({
        'id': '12345',
        'summary': '',
        'description': '',
        'location': '',
        'start': {'dateTime': '2020-03-04T04:15:30-08:00',
                  'timeZone': 'America/Los_Angeles'},
        'end': {'dateTime': '2020-03-04T10:15:30-08:00',
                'timeZone': 'America/Los_Angeles'}
    })
    # Event not in ical feed
    gcal_events.append({
        'id': 'bob4215832953301583316930',
        'summary': 'Test 1',
        'description': 'Descr 1',
        'location': 'Location 1',
        'start': {'dateTime': '2020-03-04T04:15:30-08:00',
                  'timeZone': 'America/Los_Angeles'},
        'end': {'dateTime': '2020-03-04T10:15:30-08:00',
                'timeZone': 'America/Los_Angeles'}
    })
    # Event updated in ical feed
    gcal_events.append({
        'id': 'bob4315832953301583316930',
        'summary': 'Test 2',
        'description': 'Descr 2',
        'location': 'Location 2',
        'start': {'dateTime': '2020-03-04T04:15:30-08:00',
                  'timeZone': 'America/Los_Angeles'},
        'end': {'dateTime': '2020-03-04T10:15:30-08:00',
                'timeZone': 'America/Los_Angeles'}
    })
    # Event matches ical feed
    gcal_events.append({
        'id': 'bob4415832953301583316930',
        'summary': 'Test 3',
        'description': 'Descr 3 (Imported from mycal.py)',
        'location': 'Location 3',
        'start': {'dateTime': '2020-03-04T04:15:30-08:00',
                  'timeZone': 'America/Los_Angeles'},
        'end': {'dateTime': '2020-03-04T10:15:30-08:00',
                'timeZone': 'America/Los_Angeles'}
    })

    # Now make 2 ical events, one that is updated, and one that matches.
    ical_events = {}
    begin_date = arrow.get(2020, 3, 4, 4, 15, 30)
    end_date = arrow.get(2020, 3, 4, 10, 15, 30)
    ical_events['bob4315832953301583316930'] = ics.event.Event(
        name='Test 2',
        begin=begin_date,
        end=end_date,
        uid='43',
        description='Descr 2',
        location='Updated Location')
    ical_events['bob4415832953301583316930'] = ics.event.Event(
        name='Test 3',
        begin=begin_date,
        end=end_date,
        uid='44',
        description='Descr 3',
        location='Location 3')

    delete_or_update_gcal_events(
        gcal_events=gcal_events, gcal_id='42', gcal_service=service,
        gcal_tz='America/Los_Angeles',
        ical_events=ical_events, event_id_prefix='bob')

    # All ical events were processed by this call.
    assert len(ical_events) == 0
    # All gcal api calls were made.
    assert len(http._iterable) == 0
    # Check log messages to see if we did what we said we would.
    assert len(caplog.records) == 5
    log_msgs = [r.getMessage() for r in caplog.records]
    assert '> Deleting event "Test 1" from Google Calendar...' in log_msgs
    assert '> Updating event "bob4315832953301583316930" due to change...' in \
        log_msgs
