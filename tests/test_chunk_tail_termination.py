import unittest
from knowledge_bridge.core import split_long


class ChunkTailTests(unittest.TestCase):
    def test_long_paragraph_has_full_coverage_and_one_overlap_per_boundary(self):
        text=''.join(chr(0x4e00+i) for i in range(4000))
        chunks=split_long(text,1800,180)
        self.assertEqual([1800,1800,760],list(map(len,chunks)))
        self.assertEqual(text,chunks[0]+''.join(chunk[180:] for chunk in chunks[1:]))
        for left,right in zip(chunks,chunks[1:]):self.assertEqual(left[-180:],right[:180])

    def test_one_character_over_limit_does_not_repeat_the_tail(self):
        self.assertEqual([1800,181],list(map(len,split_long('甲'*1801,1800,180))))
        self.assertEqual([1800],list(map(len,split_long('甲'*1800,1800,180))))

    def test_sentence_boundary_and_paragraph_end_preserve_text(self):
        text='甲'*1000+'。'+'乙'*1200+'。'
        chunks=split_long(text,1800,180)
        self.assertEqual(2,len(chunks))
        self.assertEqual(text,chunks[0]+chunks[1][180:])
        self.assertTrue(chunks[-1].endswith('。'))

    def test_short_paragraphs_and_empty_text_remain_unchanged(self):
        self.assertEqual(['第一段\n\n第二段'],split_long('第一段\n\n第二段',1800,180))
        self.assertEqual([],split_long('',1800,180))


if __name__=='__main__':unittest.main()
