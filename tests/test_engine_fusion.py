from app.services.parsing.engine_fusion import RowCandidate, fuse_row_candidates


def test_fusion_allows_strong_label_value_merge():
    decision = fuse_row_candidates(
        [
            RowCandidate(
                source_engine="native_pdf_table_engine",
                source_page=11,
                source_table_id="1:11:0",
                label_text="Revenue",
                statement_family="income_statement",
                row_kind="statement_line_item",
                confidence=0.9,
            ),
            RowCandidate(
                source_engine="ocr_table_structure_engine",
                source_page=11,
                source_table_id="1:11:0",
                current_value=100.0,
                comparative_value=90.0,
                effective_period="2021Q4",
                comparative_period="2020Q4",
                statement_family="income_statement",
                row_kind="statement_line_item",
                confidence=0.8,
            ),
        ],
        engine_priority={"native_pdf_table_engine": 100, "ocr_table_structure_engine": 40},
    )

    assert decision.allowed is True
    assert decision.fusion_status == "merged_engines"
    assert decision.merged_candidate.label_text == "Revenue"
    assert decision.merged_candidate.current_value == 100.0


def test_fusion_rejects_weak_alignment():
    decision = fuse_row_candidates(
        [
            RowCandidate(
                source_engine="native_pdf_table_engine",
                source_page=11,
                source_table_id="1:11:0",
                label_text="Revenue",
                statement_family="income_statement",
                row_kind="statement_line_item",
                confidence=0.9,
            ),
            RowCandidate(
                source_engine="ocr_table_structure_engine",
                source_page=11,
                source_table_id="1:11:0",
                label_text="Operating profit",
                current_value=100.0,
                effective_period="2021Q4",
                comparative_period="2020Q4",
                statement_family="income_statement",
                row_kind="statement_line_item",
                confidence=0.8,
            ),
        ]
    )

    assert decision.allowed is False
    assert decision.fusion_status == "conflict_retained_as_evidence"
    assert decision.reason in {"weak_alignment", "label_ownership_conflict", "row_alignment_conflict"}


def test_fusion_rejects_period_conflict():
    decision = fuse_row_candidates(
        [
            RowCandidate(
                source_engine="native_pdf_table_engine",
                source_page=11,
                source_table_id="1:11:0",
                label_text="Revenue",
                current_value=100.0,
                effective_period="2021Q4",
                statement_family="income_statement",
                row_kind="statement_line_item",
                confidence=0.9,
            ),
            RowCandidate(
                source_engine="ocr_table_structure_engine",
                source_page=11,
                source_table_id="1:11:0",
                label_text="Revenue",
                current_value=100.0,
                effective_period="2020Q4",
                statement_family="income_statement",
                row_kind="statement_line_item",
                confidence=0.8,
            ),
        ]
    )

    assert decision.allowed is False
    assert decision.reason == "period_conflict"


def test_fusion_rejects_sign_conflict():
    decision = fuse_row_candidates(
        [
            RowCandidate(
                source_engine="native_pdf_table_engine",
                source_page=14,
                source_table_id="1:14:0",
                label_text="Operating cash flow",
                current_value=100.0,
                sign_hint="positive",
                effective_period="2021Q4",
                statement_family="cash_flow",
                row_kind="statement_line_item",
                confidence=0.9,
            ),
            RowCandidate(
                source_engine="ocr_table_structure_engine",
                source_page=14,
                source_table_id="1:14:0",
                label_text="Operating cash flow",
                current_value=-100.0,
                sign_hint="negative",
                effective_period="2021Q4",
                statement_family="cash_flow",
                row_kind="statement_line_item",
                confidence=0.8,
            ),
        ]
    )

    assert decision.allowed is False
    assert decision.reason == "sign_conflict"


def test_ocr_only_row_carries_lower_trust_warning():
    decision = fuse_row_candidates(
        [
            RowCandidate(
                source_engine="ocr_text_engine",
                source_page=7,
                source_table_id="1:7:0",
                label_text="Revenue",
                current_value=100.0,
                effective_period="2021Q4",
                statement_family="income_statement",
                row_kind="statement_line_item",
                confidence=0.7,
            )
        ]
    )

    assert decision.allowed is True
    assert decision.warning == "ocr_only_lower_trust"


def test_native_and_ocr_agreement_merges_with_stronger_provenance():
    decision = fuse_row_candidates(
        [
            RowCandidate(
                source_engine="native_pdf_table_engine",
                source_page=11,
                source_table_id="1:11:0",
                label_text="Revenue",
                current_value=100.0,
                effective_period="2021Q4",
                statement_family="income_statement",
                row_kind="statement_line_item",
                confidence=0.85,
            ),
            RowCandidate(
                source_engine="ocr_text_engine",
                source_page=11,
                source_table_id="1:11:0",
                label_text="Revenue",
                current_value=100.0,
                effective_period="2021Q4",
                statement_family="income_statement",
                row_kind="statement_line_item",
                confidence=0.8,
            ),
        ]
    )

    assert decision.allowed is True
    assert decision.fusion_status == "merged_engines"
    assert set(decision.source_engines_involved) == {"native_pdf_table_engine", "ocr_text_engine"}
