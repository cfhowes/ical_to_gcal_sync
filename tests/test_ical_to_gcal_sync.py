import os

import arrow
from googleapiclient.discovery import build
from googleapiclient.http import HttpMockSequence
import pytest

from ..ical_to_gcal_sync import *
from .. import config

from .mock_ics_feed_response import MOCK_ICS_FEED_RESPONSE
from . import mock_gcal_responses


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
