import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_PATH = REPOSITORY_ROOT / "jenkins_scripts" / "SendMail" / "main.html"


class MailTemplateTests(unittest.TestCase):
    def test_runtime_fields_are_placeholders(self) -> None:
        template = TEMPLATE_PATH.read_text(encoding="utf-8")

        for placeholder in (
            "{{PIPELINE_STATUS}}",
            "{{PROJECT_NAME}}",
            "{{JIRA_TICKET}}",
            "{{JIRA_ISSUE_URL}}",
            "{{BLACKDUCK_BUTTONS}}",
        ):
            self.assertIn(placeholder, template)

        self.assertNotIn("OAT-2", template)
        self.assertNotIn("Nissan", template)
        self.assertNotIn("browse/OAT-2", template)


if __name__ == "__main__":
    unittest.main()
