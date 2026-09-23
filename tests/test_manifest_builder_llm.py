import sys
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "jenkins_scripts"))

from manifest_builder_llm import (  # noqa: E402
    DEFAULT_SYSTEM_PROMPT_PATH,
    extract_excluded_scan_paths,
    load_system_prompt,
    normalize_manifest,
)


class ManifestBuilderLlmTests(unittest.TestCase):
    def test_system_prompt_is_external_and_non_empty(self) -> None:
        prompt = load_system_prompt(DEFAULT_SYSTEM_PROMPT_PATH)

        self.assertIn("Return JSON only", prompt)
        self.assertIn("excluded_scan_paths", prompt)

    def test_natural_language_artifacts_are_removed_from_scan_paths(self) -> None:
        source_text = """
        참고사항
        [OSS 검출 파일에 대한 ignore 검토]
        1. File path : MakeSupport/util/vcxproj_10_HERE.sh :
        2. File path : renault_gen3_appl/DemoComponents/vBRS/GeneratorMsr/vBRS.jar
        ***사전검토서**
        """
        data = {
            "request": {
                "oem": "Renault Gen3",
                "project_name": "Renault WCBS GEN3",
                "sw_version": "SWEET500",
            },
            "scan_units": [
                {
                    "name": "APPL",
                    "scan_paths": [
                        "appl",
                        "ThirdParty/Mcal_S32k/Supply",
                        "https:/teamforge.example.com/project/tree/path",
                        "MakeSupport/util/vcxproj_10_HERE.sh",
                        "renault_gen3_appl/DemoComponents/vBRS/GeneratorMsr/vBRS.jar",
                    ],
                }
            ],
        }

        self.assertEqual(
            extract_excluded_scan_paths(source_text),
            {
                "makesupport/util/vcxproj_10_here.sh",
                "renault_gen3_appl/democomponents/vbrs/generatormsr/vbrs.jar",
            },
        )
        normalized = normalize_manifest(data, source_text=source_text)

        self.assertEqual(
            normalized["scan_units"][0]["scan_paths"],
            ["ThirdParty/Mcal_S32k/Supply"],
        )

    def test_relative_children_are_joined_to_explicit_parent(self) -> None:
        data = {
            "request": {},
            "scan_units": [
                {
                    "name": "APPL",
                    "scan_paths": [
                        "ThirdParty/Extension_Modules/Supply/Tresos/plugins/",
                        "ComR_TS_TxDxM1I8R0/",
                        "SaSrv_TS_TxDxM3I3R0/",
                        "ThirdParty/Mcal_S32k/Supply/",
                        "HSE_FW_S32K312_0_2_40_0/",
                    ],
                }
            ],
        }

        normalized = normalize_manifest(data)

        self.assertEqual(
            normalized["scan_units"][0]["scan_paths"],
            [
                "ThirdParty/Extension_Modules/Supply/Tresos/plugins/ComR_TS_TxDxM1I8R0/",
                "ThirdParty/Extension_Modules/Supply/Tresos/plugins/SaSrv_TS_TxDxM3I3R0/",
                "ThirdParty/Mcal_S32k/Supply/HSE_FW_S32K312_0_2_40_0/",
            ],
        )


if __name__ == "__main__":
    unittest.main()
