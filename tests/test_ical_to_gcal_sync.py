import pytest

from ..ical_to_gcal_sync import *
from .. import config

from .mock_ics_feed_response import MOCK_ICS_FEED_RESPONSE


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
