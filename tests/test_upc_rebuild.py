from services.analytics.file_validation import inspect_barcode, validate_catalog
from services.analytics.parser import SourceRow


def test_ten_digit_upc_rebuilt_with_leading_zero_and_check_digit():
    # real Church & Dwight rows; the rebuilt UPCs are on Amazon listings (Arm & Hammer Baking Soda 8oz, ...)
    assert inspect_barcode("3320001140") == ("rebuilt", "033200011408")
    assert inspect_barcode("3320000020") == ("rebuilt", "033200000204")


def test_rejected_upc_keeps_its_original_value():
    rows = [SourceRow(row_idx=0, upc="12345", upc_original="12345", title="x"),
            SourceRow(row_idx=1, upc="3320001140", upc_original="3320001140", title="y")]
    cleaned, report = validate_catalog(rows)
    assert cleaned[0].upc == "" and cleaned[0].upc_original == "12345"     # not searched, still shown
    assert cleaned[1].upc == "033200011408"
    codes = {i["code"] for i in report.as_dict()["issues"]}
    assert {"upc_bad_length", "upc_rebuilt"} <= codes
