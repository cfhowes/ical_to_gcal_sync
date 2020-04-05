from dateutil.tz import tzutc
import os

import arrow
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
