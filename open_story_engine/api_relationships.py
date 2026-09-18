"""Player-visible links supported by the selected route's narration only."""
import re
from .cocreation import narration_outside_dialogue


def known_relationships(nodes, people, player_name):
    # Do not read the package relationship catalog: it may describe future scenes.
    links = {}
    if nodes:
        root = nodes[0]
        known = {p['name']: p['id'] for p in people if p['first_page'] <= root['sequence'] + 1}
        # Only the saved, reviewed opening context is public here, not the
        # package's full relationship catalog or another character's opening.
        for item in (root.get('openingContext') or {}).get('relationships', []):
            name, relation = item.get('name'), item.get('relation')
            if (player_name not in known or name not in known or name == player_name
                    or name not in root.get('narrativeText', '') or not isinstance(relation, str) or not relation.strip()):
                continue
            source, target = known[player_name], known[name]
            label = re.split(r'[，。；]', relation)[0]
            links[tuple(sorted((source, target)))] = {
                'source': source, 'target': target, 'label': label if len(label) <= 10 else '已知关系',
                'evidence': relation, 'page': root['sequence'] + 1, 'origin': 'opening',
            }
    for node in nodes:
        prose = narration_outside_dialogue(node.get('narrativeText', ''))
        for sentence in re.split(r'[。！？\n]', prose):
            if re.search(r'如果|假如|也许|可能|想起|回忆|录音(?:笔)?里|语音里', sentence):
                continue
            names = {person['name']: person['id'] for person in people
                     if node['sequence'] + 1 >= person['first_page']}
            if player_name in names:
                names['你'] = names[player_name]
            if not names:
                continue
            pattern = '|'.join(re.escape(name) + ('(?!们)' if name == '你' else '') for name in sorted(names, key=len, reverse=True))
            present = [(match.start(), match.end(), names[match[0]]) for match in re.finditer(pattern, sentence)]
            # Adjacent mentions with a direct relation/action, not mere co-occurrence.
            for left, right in zip(present, present[1:]):
                if left[2] == right[2]:
                    continue
                between = sentence[left[1]:right[0]]
                if len(between) > 32 or re.search(r'不|没|未|并非|不是|无意|拒绝', between):
                    continue
                after = sentence[right[1]:]
                formal = re.match(r'(?:是|原本是|一直是|这位原本的|这位|这个)(?:你的|他的|她的)?(?:老)?(朋友|好友|同事|同僚)', after)
                label = next((label for pattern, label in (
                    (r'朋友|好友', '朋友'), (r'同事|同僚', '同事'),
                    (r'扶|搀|救|帮|协助|护住', '协助'),
                    (r'拦|阻止|争执|反驳|质问', '交锋'),
                    (r'问|询问|交谈|回应|回答|告诉|叫住', '交谈'),
                    (r'看见|看到|看向|看着|望向|转向|走向|身旁|身边', '相遇'),
                ) if re.search(pattern, between)), None)
                if formal:
                    label = '朋友' if formal[1] in ('朋友', '好友') else '同事'
                if label:
                    key = tuple(sorted((left[2], right[2])))
                    previous = links.get(key)
                    # Keep a meaningful established relation over a later glance.
                    if previous and (label == '相遇' or previous['label'] in ('朋友', '同事')):
                        continue
                    links[key] = {'source': left[2], 'target': right[2], 'label': label,
                                  'evidence': sentence.strip(), 'page': node['sequence'] + 1}
        # A direct accusation can live in dialogue, which the conservative
        # narration pass above intentionally excludes.
        names = {p['name']: p['id'] for p in people if p['first_page'] <= node['sequence'] + 1}
        if player_name in names:
            names['你'] = names[player_name]
        if names:
            actor = '(?:' + '|'.join(map(re.escape, names)) + ')'
            for paragraph in node.get('narrativeText', '').split('\n\n'):
                match = re.search('(' + actor + r')[^。！？“]{0,16}(?:看|问|质问|质疑)(' + actor + r')[：:，,]', paragraph)
                if match and names[match[1]] != names[match[2]] and re.search(r'“[^”]*(?:你改过|你还要|为什么|你早知道|你把)', paragraph):
                    key = tuple(sorted((names[match[1]], names[match[2]])))
                    links[key] = {'source': names[match[1]], 'target': names[match[2]], 'label': '质疑',
                                  'evidence': paragraph, 'page': node['sequence'] + 1}
        # Apply notes in chronological order, so an older note cannot overwrite
        # a newer encounter. Both endpoints must already be known on this route.
        known = {p['name']: p['id'] for p in people if p['first_page'] <= node['sequence'] + 1}
        for item in node.get('readerOutcome', {}).get('relationships', []):
            if (item.get('source') in known and item.get('target') in known and item['source'] != item['target']
                    and item.get('evidence') and item['evidence'] in node.get('narrativeText', '')):
                source, target = known[item['source']], known[item['target']]
                links[tuple(sorted((source, target)))] = {**item, 'source': source, 'target': target, 'page': node['sequence'] + 1}
    return list(links.values())
