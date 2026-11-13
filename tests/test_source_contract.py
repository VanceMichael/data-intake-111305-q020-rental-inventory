import hashlib
import json
import unittest

from rental_inventory.client import Client
from rental_inventory.decoding import parse_page, records as decode_records
from rental_inventory.origin import load_scenario, serve


class SourceContractTests(unittest.TestCase):
    def test_domain_observations_preserve_source_distinctions(self):
        scenario = load_scenario()
        records = scenario["source_records"]
        with serve(scenario) as origin:
            client = Client(origin.base_url)
            self.assertEqual(records[0]["equipment"], records[1]["equipment"])
            self.assertNotEqual(records[0]["granularity"], records[1]["granularity"])
            self.assertEqual(records[1]["start"], records[2]["start"])
            self.assertNotEqual(records[1]["status"], records[2]["status"])

    def test_every_record_has_the_declared_source_identity(self):
        scenario = load_scenario()
        for record in scenario["source_records"]:
            for field in scenario["identity_fields"]:
                self.assertIn(field, record)
                self.assertNotEqual(str(record[field]).strip(), "")


if __name__ == "__main__":
    unittest.main()
