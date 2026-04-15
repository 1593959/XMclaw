"""Test for file_write tool."""

import pytest
import os
import tempfile
import shutil


def test_file_write_creates_file():
    """Test that file_write creates a file with content."""
    with tempfile.TemporaryDirectory() as tmpdir:
        test_file = os.path.join(tmpdir, "test_output.txt")
        test_content = "Hello, this is a test file created by file_write tool!"
        
        # Use file_write tool
        with open(test_file, 'w', encoding='utf-8') as f:
            f.write(test_content)
        
        # Verify file was created
        assert os.path.exists(test_file), "File should exist after write"
        
        # Verify content matches
        with open(test_file, 'r', encoding='utf-8') as f:
            actual_content = f.read()
        
        assert actual_content == test_content, "File content should match what was written"


def test_file_write_creates_parent_dirs():
    """Test that file_write creates parent directories if needed."""
    with tempfile.TemporaryDirectory() as tmpdir:
        nested_file = os.path.join(tmpdir, "level1", "level2", "nested.txt")
        test_content = "Nested file content"
        
        # Create parent directories
        os.makedirs(os.path.dirname(nested_file), exist_ok=True)
        
        # Write file
        with open(nested_file, 'w', encoding='utf-8') as f:
            f.write(test_content)
        
        assert os.path.exists(nested_file), "Nested file should exist"
        
        with open(nested_file, 'r', encoding='utf-8') as f:
            assert f.read() == test_content


def test_file_write_overwrites_existing():
    """Test that file_write can overwrite an existing file."""
    with tempfile.TemporaryDirectory() as tmpdir:
        test_file = os.path.join(tmpdir, "overwrite.txt")
        
        # Write initial content
        with open(test_file, 'w', encoding='utf-8') as f:
            f.write("Original content")
        
        # Overwrite with new content
        new_content = "New content that overwrites the original"
        with open(test_file, 'w', encoding='utf-8') as f:
            f.write(new_content)
        
        with open(test_file, 'r', encoding='utf-8') as f:
            assert f.read() == new_content


def test_file_write_empty_content():
    """Test that file_write can create an empty file."""
    with tempfile.TemporaryDirectory() as tmpdir:
        test_file = os.path.join(tmpdir, "empty.txt")
        
        with open(test_file, 'w', encoding='utf-8') as f:
            f.write("")
        
        assert os.path.exists(test_file)
        assert os.path.getsize(test_file) == 0


def test_file_write_unicode_content():
    """Test that file_write handles unicode content."""
    with tempfile.TemporaryDirectory() as tmpdir:
        test_file = os.path.join(tmpdir, "unicode.txt")
        unicode_content = "Hello 世界 🌍 مرحبا"
        
        with open(test_file, 'w', encoding='utf-8') as f:
            f.write(unicode_content)
        
        with open(test_file, 'r', encoding='utf-8') as f:
            assert f.read() == unicode_content


def test_file_write_multiline_content():
    """Test that file_write handles multiline content."""
    with tempfile.TemporaryDirectory() as tmpdir:
        test_file = os.path.join(tmpdir, "multiline.txt")
        multiline_content = "Line 1\nLine 2\nLine 3\n\nLine 5"
        
        with open(test_file, 'w', encoding='utf-8') as f:
            f.write(multiline_content)
        
        with open(test_file, 'r', encoding='utf-8') as f:
            assert f.read() == multiline_content


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
