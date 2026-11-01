from puente.ranking import ProviderProfile, rank_providers, reliability


def P(id, **kw):
    base = dict(id=id, display_name=f"P{id}", kyc_level=2, status="active", paypal_verified=True,
                completed_orders=50, disputed_orders=0, failed_orders=0, avg_settle_minutes=15,
                usd_capacity_available=1000, margin_pct=2.0, fee_usd=0.0, accepts_banks={"0102"})
    base.update(kw)
    return ProviderProfile(**base)


def rank(providers, **kw):
    args = dict(amount_usd=50, bank="0102", ref_rate=40.0, min_kyc=1)
    args.update(kw)
    return rank_providers(providers, **args)


def test_hard_filters_with_reasons():
    res = rank([P(1), P(2, status="paused"), P(3, kyc_level=0), P(4, usd_capacity_available=10),
                P(5, accepts_banks={"0105"}), P(6, paypal_verified=False),
                P(7, completed_orders=30, disputed_orders=12)])
    assert [q.provider_id for q in res.quotes] == [1]
    assert res.rejected == {2: "inactive", 3: "kyc_too_low", 4: "no_capacity", 5: "bank_unsupported",
                            6: "paypal_unverified", 7: "high_dispute_rate"}


def test_cheaper_wins_when_otherwise_equal():
    res = rank([P(1, margin_pct=4.0), P(2, margin_pct=1.0)])
    assert res.quotes[0].provider_id == 2 and res.quotes[0].rank == 1


def test_total_ves_math():
    q = rank([P(1, margin_pct=10.0, fee_usd=1.0)]).quotes[0]
    assert q.rate_ves_per_usd == 44.0 and q.total_ves == round((50 + 1) * 44.0, 2)


def test_reliability_beats_tiny_price_edge():
    veteran = P(1, margin_pct=2.0, completed_orders=200)
    risky_new = P(2, margin_pct=1.9, completed_orders=1, failed_orders=2, avg_settle_minutes=50)
    assert rank([veteran, risky_new]).quotes[0].provider_id == 1


def test_smoothing_new_provider_not_perfect():
    assert reliability(P(1, completed_orders=1)) < reliability(P(2, completed_orders=200))


def test_exclude_and_top_n_and_empty():
    res = rank([P(1), P(2), P(3), P(4)], exclude={1}, top_n=2)
    assert len(res.quotes) == 2 and res.rejected[1] == "excluded_previous_failure"
    assert rank([P(1, status="banned")]).quotes == []


def test_score_in_range_and_breakdown_sums():
    q = rank([P(1)]).quotes[0]
    assert 0 <= q.score <= 100 and set(q.breakdown) == {"price", "reliability", "speed", "disputes", "kyc", "capacity"}
