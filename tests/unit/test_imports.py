def test_package_imports_cleanly():
    import model  # noqa: F401
    import model.data  # noqa: F401
