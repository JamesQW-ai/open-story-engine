import unittest
from open_story_engine.cocreation import _new_location_reference


class LocationReferenceTests(unittest.TestCase):
    def test_unknown_and_negated_locations_are_not_discoveries(self):
        for text, name in [
            ('后一件，你连隧道入口在哪儿都不知道。', '你连隧道'),
            ('你不知道隧道入口在哪里。', '隧道'),
            ('你没有找到维修通道。', '维修通道'),
            ('你没有找到地下密室的入口。', '地下密室'),
            ('你无法进入旧机房。', '旧机房'),
        ]:
            with self.subTest(text=text):
                self.assertFalse(_new_location_reference(text, name))

    def test_affirmative_places_remain_guarded(self):
        for text, name in [
            ('你找到了废弃机房的入口。', '废弃机房'),
            ('你进入了地下密室。', '地下密室'),
            ('你没有找到维修通道，但随后进入了地下密室。', '地下密室'),
            ('眼前是一间地下密室，你走进了里面。', '地下密室'),
            ('你不知道里面有什么，却走进了地下密室。', '地下密室'),
        ]:
            with self.subTest(text=text):
                self.assertTrue(_new_location_reference(text, name))

    def test_reporting_train_arrival_does_not_relocate_speaker(self):
        from open_story_engine.cocreation import guard_unbound_source_outcomes
        package = {'metadata': {'authoringSource': 'source_text_script'},
                   'characters': [{'id': 'chen', 'name': '陈砚'}]}
        state = {'storyScope': 'source'}
        guard_unbound_source_outcomes('陈砚说末班车到站那会儿人很多。', package, state)
        for text in ['陈砚刚刚到站。', '陈砚说自己已经到站。', '陈砚来到地下密室。']:
            with self.subTest(text=text), self.assertRaises(ValueError):
                guard_unbound_source_outcomes(text, package, state)

    def test_describing_or_planning_a_route_does_not_move_the_speaker(self):
        from open_story_engine.cocreation import guard_character_final_locations
        package = {'characters': [{'id': 'jiang', 'name': '姜序'}],
                   'locations': [{'id': 'hall', 'name': '候车厅'}, {'id': 'office', 'name': '站务室'}]}
        state = {'characterLocationIds': {'jiang': 'hall'}}
        for text in ['姜序说明可进入站务室的路线。', '姜序打算进入站务室。', '姜序没有进入站务室。']:
            guard_character_final_locations(text, package, state)
        for text in ['姜序进入站务室。', '姜序犹豫片刻，随后进入站务室。']:
            with self.subTest(text=text), self.assertRaises(ValueError):
                guard_character_final_locations(text, package, state)
