"""Tests for Google Analytics view-only service.

Verifies:
- Access control: Unauthenticated redirected to login.
- Access control: Authenticated members without FEATURE_ANALYTICS get 403 Forbidden.
- Access control: Navigation link is hidden when ungranted and visible when granted or admin.
- Access control: Members with FEATURE_ANALYTICS or administrators get 200 OK.
- Service account key handling & metadata browsing.
"""

import pytest

from mizalla.extensions import db
from mizalla.feature_access import sync_member_features
from mizalla.models import FEATURE_ANALYTICS, Member

PASSWORD = "member-password-long"


def make_member(name="User", email="user@example.com", **kwargs):
    member = Member(name=name, email=email, **kwargs)
    member.set_password(PASSWORD)
    db.session.add(member)
    db.session.commit()
    return member


def login(client, email="user@example.com"):
    return client.post("/apps/login", data={"email": email, "password": PASSWORD})


def test_analytics_requires_authentication(client):
    """Anonymous visitors are redirected to the login page."""
    response = client.get("/apps/analytics/")
    assert response.status_code == 302
    assert "/apps/login" in response.headers["Location"]


def test_analytics_forbidden_to_ungranted_members(app, client):
    """Authenticated members without the analytics feature grant receive 403 Forbidden."""
    with app.app_context():
        make_member("Regular Member", "member@example.com")

    login(client, "member@example.com")
    response = client.get("/apps/analytics/")
    assert response.status_code == 403
    assert "You do not have access to Google Analytics" in response.get_data(as_text=True)


def test_analytics_link_hidden_in_nav_for_ungranted_members(app, client):
    """A member without the grant does not see the Analytics link in the navigation header."""
    with app.app_context():
        make_member("Regular Member", "member@example.com")

    login(client, "member@example.com")
    page = client.get("/apps/projects").get_data(as_text=True)
    assert "/apps/analytics" not in page


def test_analytics_accessible_to_granted_members(app, client, credentials):
    """Members who have been granted FEATURE_ANALYTICS see the link and can access the service."""
    credentials.set()
    with app.app_context():
        member = make_member("Analyst", "analyst@example.com")
        sync_member_features(member, [FEATURE_ANALYTICS])
        db.session.commit()

    login(client, "analyst@example.com")
    
    # Check navigation link visibility
    page = client.get("/apps/projects").get_data(as_text=True)
    assert "/apps/analytics" in page

    # Access the service dashboard
    response = client.get("/apps/analytics/")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "Google Analytics Data Explorer" in html
    assert "Service Account Authentication" in html


def test_analytics_accessible_to_administrators(app, client, credentials):
    """Administrators inherently pass has_feature_access and can access the analytics service."""
    credentials.set()
    with app.app_context():
        make_member("Admin User", "admin@example.com", is_admin=True)

    login(client, "admin@example.com")
    
    page = client.get("/apps/projects").get_data(as_text=True)
    assert "/apps/analytics" in page

    response = client.get("/apps/analytics/")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "Google Analytics Data Explorer" in html


def test_google_analytics_property_model_and_access(app):
    """Test GoogleAnalyticsProperty model and accessible_properties_for method."""
    from mizalla.models import GoogleAnalyticsProperty

    with app.app_context():
        user1 = make_member("User One", "user1@example.com")
        user2 = make_member("User Two", "user2@example.com")
        admin = make_member("Admin One", "admin1@example.com", is_admin=True)

        prop_a = GoogleAnalyticsProperty(
            property_id="12345678",
            display_name="Website A",
            account_name="Main Account",
        )
        prop_b = GoogleAnalyticsProperty(
            property_id="87654321",
            display_name="Website B",
            account_name="Secondary Account",
        )
        prop_all = GoogleAnalyticsProperty(
            property_id="99999999",
            display_name="Global Website",
            all_members=True,
        )

        db.session.add_all([prop_a, prop_b, prop_all])
        prop_a.authorized_members.append(user1)
        db.session.commit()

        # User 1 should see prop_a and prop_all
        user1_props = GoogleAnalyticsProperty.accessible_properties_for(user1)
        user1_prop_ids = {p.property_id for p in user1_props}
        assert user1_prop_ids == {"12345678", "99999999"}

        # User 2 should only see prop_all
        user2_props = GoogleAnalyticsProperty.accessible_properties_for(user2)
        user2_prop_ids = {p.property_id for p in user2_props}
        assert user2_prop_ids == {"99999999"}

        # Admin should see all properties
        admin_props = GoogleAnalyticsProperty.accessible_properties_for(admin)
        admin_prop_ids = {p.property_id for p in admin_props}
        assert admin_prop_ids == {"12345678", "87654321", "99999999"}


def test_discover_properties_parses_account_summaries(monkeypatch):
    """Test that discover_properties queries Admin API and extracts properties correctly."""
    from mizalla import google_analytics

    sample_response = {
        "accountSummaries": [
            {
                "name": "accountSummaries/1001",
                "account": "accounts/1001",
                "displayName": "Corporate HQ",
                "propertySummaries": [
                    {
                        "property": "properties/111222333",
                        "displayName": "HQ Main Portal",
                        "propertyType": "PROPERTY_TYPE_ORDINARY",
                    },
                    {
                        "property": "properties/444555666",
                        "displayName": "HQ Mobile App",
                        "propertyType": "PROPERTY_TYPE_ORDINARY",
                    },
                ],
            },
            {
                "name": "accountSummaries/2002",
                "account": "accounts/2002",
                "displayName": "Regional Branch",
                "propertySummaries": [
                    {
                        "property": "properties/777888999",
                        "displayName": "Regional Microsite",
                        "propertyType": "PROPERTY_TYPE_ORDINARY",
                    }
                ],
            },
        ]
    }

    monkeypatch.setattr(google_analytics, "get_access_token", lambda: "mock-token-xyz")
    
    class MockResponse:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def read(self):
            import json
            return json.dumps(sample_response).encode("utf-8")

    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", lambda req: MockResponse())

    discovered = google_analytics.discover_properties()
    assert len(discovered) == 3
    assert discovered[0] == {
        "property_id": "111222333",
        "display_name": "HQ Main Portal",
        "account_name": "Corporate HQ",
    }
    assert discovered[2] == {
        "property_id": "777888999",
        "display_name": "Regional Microsite",
        "account_name": "Regional Branch",
    }


def test_fetch_executive_analytics_parses_report(monkeypatch):
    """Test fetch_executive_analytics returns formatted KPI cards and page list."""
    from mizalla import google_analytics

    def mock_run_report(property_id, spec):
        dimensions = [d["name"] for d in spec.get("dimensions", [])]
        if "pagePath" in dimensions:
            # Table of pages
            return {
                "rows": [
                    {
                        "dimensionValues": [{"value": "/home"}, {"value": "Homepage"}],
                        "metricValues": [{"value": "1500"}, {"value": "800"}, {"value": "0.35"}],
                    },
                    {
                        "dimensionValues": [{"value": "/pricing"}, {"value": "Pricing Plan"}],
                        "metricValues": [{"value": "450"}, {"value": "300"}, {"value": "0.42"}],
                    },
                ]
            }
        elif "country" in dimensions:
            # Top countries
            return {
                "rows": [
                    {"dimensionValues": [{"value": "United States"}], "metricValues": [{"value": "1200"}]},
                    {"dimensionValues": [{"value": "United Kingdom"}], "metricValues": [{"value": "350"}]},
                ]
            }
        elif "date" in dimensions:
            # Daily stats for min/max
            return {
                "rows": [
                    {
                        "dimensionValues": [{"value": "20261001"}],
                        "metricValues": [{"value": "500"}, {"value": "200"}, {"value": "120.0"}, {"value": "2.5"}],
                    },
                    {
                        "dimensionValues": [{"value": "20261002"}],
                        "metricValues": [{"value": "800"}, {"value": "250"}, {"value": "180.0"}, {"value": "3.2"}],
                    },
                ]
            }
        else:
            # Overview KPIs
            return {
                "rows": [
                    {
                        "metricValues": [
                            {"value": "1300"},  # screenPageViews
                            {"value": "450"},   # sessions
                            {"value": "300"},   # newUsers
                            {"value": "400"},   # activeUsers
                            {"value": "0.38"},  # bounceRate
                            {"value": "150.5"}, # averageSessionDuration
                            {"value": "2.88"},  # screenPageViewsPerSession
                        ]
                    }
                ]
            }

    monkeypatch.setattr(google_analytics, "run_report", mock_run_report)

    report = google_analytics.fetch_executive_analytics("111222333", start_date="30daysAgo", end_date="today")
    assert report["page_views"] == 1300
    assert report["sessions"] == 450
    assert report["new_sessions"] == 300
    assert report["returning_users"] == 100
    assert report["bounce_rate"] == 38.0
    assert report["avg_session_duration_sec"] == 150.5
    assert report["page_views_per_session_min"] == 2.5
    assert report["page_views_per_session_max"] == 3.2
    assert report["session_length_min_sec"] == 120.0
    assert report["session_length_max_sec"] == 180.0
    assert len(report["top_countries"]) == 2
    assert report["top_countries"][0]["country"] == "United States"
    assert len(report["pages"]) == 2
    assert report["pages"][0]["path"] == "/home"
    assert report["pages"][0]["views"] == 1500


def test_admin_analytics_properties_access_control(app, client):
    """Non-admin members cannot access admin analytics property views."""
    with app.app_context():
        make_member("Regular User", "regular@example.com")

    login(client, "regular@example.com")
    resp = client.get("/apps/admin/analytics-properties")
    assert resp.status_code == 403


def test_admin_analytics_properties_sync_and_manage(app, client, monkeypatch):
    """Admin can sync discovered properties and update member access."""
    from mizalla import google_analytics
    from mizalla.models import GoogleAnalyticsProperty

    with app.app_context():
        admin = make_member("Admin", "admin@example.com", is_admin=True)
        analyst = make_member("Analyst", "analyst@example.com")

    login(client, "admin@example.com")

    # Mock discovery
    mock_discovered = [
        {"property_id": "1001", "display_name": "Portal Main", "account_name": "Acme Corp"},
        {"property_id": "1002", "display_name": "Shop Site", "account_name": "Acme Corp"},
    ]
    monkeypatch.setattr(google_analytics, "discover_properties", lambda: mock_discovered)

    # 1. Trigger sync
    sync_resp = client.post("/apps/admin/analytics-properties/sync", follow_redirects=True)
    assert sync_resp.status_code == 200

    with app.app_context():
        p1 = GoogleAnalyticsProperty.query.filter_by(property_id="1001").first()
        p2 = GoogleAnalyticsProperty.query.filter_by(property_id="1002").first()
        assert p1 is not None and p1.display_name == "Portal Main"
        assert p2 is not None

        # 2. Grant analyst access to p1
        analyst_obj = Member.query.filter_by(email="analyst@example.com").first()
        prop1_id = p1.id
        analyst_id = analyst_obj.id

    access_resp = client.post(
        f"/apps/admin/analytics-properties/{prop1_id}/access",
        data={"member_ids": [str(analyst_id)]},
        follow_redirects=True,
    )
    assert access_resp.status_code == 200

    with app.app_context():
        p1 = db.session.get(GoogleAnalyticsProperty, prop1_id)
        assert len(p1.authorized_members) == 1
        assert p1.authorized_members[0].email == "analyst@example.com"


def test_analytics_dashboard_property_selector_and_access(app, client, monkeypatch, credentials):
    """User only sees authorized properties in selector and can query report."""
    credentials.set()
    from mizalla import google_analytics
    from mizalla.models import GoogleAnalyticsProperty

    with app.app_context():
        user = make_member("Authorized Analyst", "auth_analyst@example.com")
        sync_member_features(user, [FEATURE_ANALYTICS])
        
        prop_allowed = GoogleAnalyticsProperty(
            property_id="2001",
            display_name="Allowed Portal",
        )
        prop_forbidden = GoogleAnalyticsProperty(
            property_id="2002",
            display_name="Secret Portal",
        )
        db.session.add_all([prop_allowed, prop_forbidden])
        prop_allowed.authorized_members.append(user)
        db.session.commit()

    # Mock executive report
    monkeypatch.setattr(
        google_analytics,
        "fetch_executive_analytics",
        lambda prop_id, start_date="30daysAgo", end_date="today": {
            "property_id": prop_id,
            "start_date": start_date,
            "end_date": end_date,
            "page_views": 9999,
            "sessions": 1200,
            "new_sessions": 800,
            "returning_users": 400,
            "bounce_rate": 25.5,
            "avg_session_duration_sec": 145.0,
            "avg_session_duration_formatted": "2m 25s",
            "page_views_per_session_avg": 3.1,
            "page_views_per_session_min": 1.5,
            "page_views_per_session_max": 4.8,
            "session_length_min_sec": 60.0,
            "session_length_max_sec": 300.0,
            "session_length_min_formatted": "1m 00s",
            "session_length_max_formatted": "5m 00s",
            "top_countries": [{"country": "United States", "users": 800, "percentage": 80.0}],
            "pages": [{"path": "/home", "title": "Home", "views": 5000, "users": 1000, "bounce_rate": "20.0%"}],
        },
    )

    login(client, "auth_analyst@example.com")

    # 1. Access dashboard without property_id param -> auto-selects allowed property
    resp = client.get("/apps/analytics/")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "Allowed Portal" in html
    assert "Secret Portal" not in html
    assert "9,999" in html  # formatted page views with comma
    assert "Grid.js" in html or "gridjs" in html.lower()

    # 2. Accessing forbidden property returns 403 or redirects to permitted property
    forbidden_resp = client.get("/apps/analytics/?property_id=2002")
    assert forbidden_resp.status_code in (403, 302, 200)
    if forbidden_resp.status_code == 200:
        # If it recovers gracefully, it must not show the secret property
        assert "Secret Portal" not in forbidden_resp.get_data(as_text=True)




