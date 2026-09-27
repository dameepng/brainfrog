"""Unit tests for System 2 JSON extraction and auto-repair utilities."""
import unittest

from system2.json_utils import extract_json, repair_json_content, attempt_close_truncated_json


class TestJsonUtils(unittest.TestCase):
    def test_clean_json(self):
        raw = '{"files": {"main.py": "print(1)"}, "summary": "ok"}'
        res = extract_json(raw)
        self.assertEqual(res["summary"], "ok")
        self.assertEqual(res["files"]["main.py"], "print(1)")

    def test_markdown_fence_extraction(self):
        raw = """Here is the updated code:
```json
{
  "files": {
    "utils.ts": "export const add = (a: number, b: number) => a + b;"
  },
  "summary": "added add helper"
}
```
Hope this helps!"""
        res = extract_json(raw)
        self.assertEqual(res["summary"], "added add helper")
        self.assertIn("export const add", res["files"]["utils.ts"])

    def test_jsx_unescaped_quotes_repair(self):
        # The exact scenario encountered by the user with JSX space {" "}
        raw = r'''{
  "files": {
    "app/page.tsx": "<h1 className=\"mb-6 text-balance\">\n  Teguk Kesegaran Asli,{" "}\n  <span className=\"bg-gradient-to-r\">Enak</span>\n</h1>"
  },
  "summary": "added hero header"
}'''
        res = extract_json(raw)
        self.assertIn("app/page.tsx", res["files"])
        self.assertIn("Teguk Kesegaran Asli,{' '}", res["files"]["app/page.tsx"])
        self.assertEqual(res["summary"], "added hero header")

    def test_jsx_various_expressions(self):
        raw = r'''{
  "files": {
    "app/header.tsx": "<span>Hello, { " " } world! {"\t"} { "cool" }</span>"
  }
}'''
        res = extract_json(raw)
        content = res["files"]["app/header.tsx"]
        self.assertIn("{' '}", content)
        self.assertIn("{'cool'}", content)

    def test_trailing_commas_and_newlines(self):
        raw = """{
  "files": {
    "script.py": "def test():\\n    pass\\n",
  },
  "summary": "trailing comma test",
}"""
        res = extract_json(raw)
        self.assertEqual(res["summary"], "trailing comma test")

    def test_invalid_escape_sequences(self):
        # \d and \s are invalid JSON escapes in strict mode, but common in regex code
        raw = r'''{
  "files": {
    "validator.py": "import re\npattern = re.compile(r\"\d+\s+\w+\")\npath = \"C:\Users\User\app\""
  }
}'''
        res = extract_json(raw)
        self.assertIn("validator.py", res["files"])

    def test_truncated_json_auto_closure(self):
        raw = r'''```json
{
  "files": {
    "index.ts": "console.log('partial stream done');'''
        res = extract_json(raw)
        self.assertIn("index.ts", res["files"])
        self.assertEqual(res["files"]["index.ts"], "console.log('partial stream done');")

    def test_no_json_raises_value_error(self):
        with self.assertRaises(ValueError):
            extract_json("Just plain conversational text with no braces.")


if __name__ == "__main__":
    unittest.main()
