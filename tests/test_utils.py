from job_applier.utils import sanitize_name


def test_sanitize_name():
    # Test typical sanitization
    assert sanitize_name("Software Engineer / DevOps!") == "Software_Engineer_DevOps"
    # Test already clean string
    assert sanitize_name("Cloud_Architect") == "Cloud_Architect"
    # Test empty string
    assert sanitize_name("") == ""
    # Test only bad characters
    assert sanitize_name("@#$%^&*()") == ""
    # Test whitespace handling
    assert sanitize_name("  Data Analyst  ") == "Data_Analyst"
