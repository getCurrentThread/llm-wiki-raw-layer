# -*- coding: utf-8 -*-
"""kakaotalk's export to an LLM Wiki project (`manage.py collect kakaotalk`): a chat layout and no config keys, so its
settings come from the command line only."""
import unittest

from rawlayer import registry


class WikiExportDeclaration(unittest.TestCase):
    def test_collect_declaration(self):
        app = registry.app_registry("kakaotalk").apps[0]
        self.assertEqual(app.wiki, (None, "chat"))
        self.assertEqual([op.key for op in app.options if "_wiki_" in op.key], [])


if __name__ == "__main__":
    unittest.main()
