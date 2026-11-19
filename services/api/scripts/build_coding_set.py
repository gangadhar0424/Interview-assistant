# ruff: noqa: E501, RUF001, S101, T201, S603, PLR2004
"""Build the synthetic CODING evaluation set (M-8): tests/eval/coding_set.jsonl.

    cd services/api && python scripts/build_coding_set.py

Everything is invented: problem statements are written in this file in the common style of
interview exercises, with no text copied from any site. Each executable problem carries a reference
solution and hidden cases; tests/test_coding_assistance.py runs the reference solutions against the
cases so the set itself is checked. Written by the assistant (an AI); no human review of the
statements, references or expected complexity yet (see docs/CODING_ASSISTANCE_EVALUATION.md).

Row fields: id, category, source ("typed" | "screenshot_extracted"), prompt, language (the language
the prompt requires, or null), kind ("function" | "sql" | "none"), entry (function name), cases
([args, expected] pairs, run in order in one process), norm (how to compare a result), sql_setup,
sql_expected, time/space (reference complexity), wants_* (what a good answer contains),
must_mention (words a conceptual answer must contain), reference (a correct solution).
"""

import json
from pathlib import Path
from typing import Any

OUT = Path(__file__).resolve().parents[1] / "tests" / "eval" / "coding_set.jsonl"


def fn(
    id_: str,
    category: str,
    prompt: str,
    entry: str,
    cases: list[Any],
    reference: str,
    time: str,
    space: str,
    *,
    norm: str = "exact",
    source: str = "typed",
) -> dict[str, Any]:
    return {
        "id": id_,
        "category": category,
        "source": source,
        "prompt": prompt,
        "language": "python",
        "kind": "function",
        "entry": entry,
        "cases": cases,
        "norm": norm,
        "time": time,
        "space": space,
        "wants_code": True,
        "wants_complexity": True,
        "wants_tests": True,
        "wants_assumptions": False,
        "reference": reference,
    }


def other(id_: str, category: str, prompt: str, **extra: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": id_,
        "category": category,
        "source": "typed",
        "prompt": prompt,
        "language": None,
        "kind": "none",
        "wants_code": True,
        "wants_complexity": True,
        "wants_tests": True,
        "wants_assumptions": False,
    }
    row.update(extra)
    return row


EMP_SETUP = (
    "CREATE TABLE employees (id INTEGER PRIMARY KEY, name TEXT, salary INTEGER, dept TEXT);"
    "INSERT INTO employees VALUES (1,'Asha',5000,'eng'),(2,'Ben',7000,'eng'),(3,'Chen',7000,'ops'),"
    "(4,'Dara',4000,'ops'),(5,'Eli',6000,'eng'),(6,'Fay',3000,'hr');"
)

ROWS: list[dict[str, Any]] = [
    fn(
        "arr-two-sum",
        "arrays_strings",
        "Write a Python function two_sum(nums, target) that returns the indices [i, j] with i < j of the two numbers in the list nums that add up to target. Exactly one pair exists.",
        "two_sum",
        [
            [[[2, 7, 11, 15], 9], [0, 1]],
            [[[3, 2, 4], 6], [1, 2]],
            [[[3, 3], 6], [0, 1]],
            [[[-1, -2, -3, -4], -6], [1, 3]],
        ],
        "def two_sum(nums, target):\n    seen = {}\n    for j, n in enumerate(nums):\n        if target - n in seen:\n            return [seen[target - n], j]\n        seen[n] = j\n",
        "O(n)",
        "O(n)",
    ),
    fn(
        "str-palindrome",
        "arrays_strings",
        "Write a Python function is_palindrome(s) that returns True if the string s reads the same forwards and backwards when you ignore case and every character that is not a letter or digit.",
        "is_palindrome",
        [
            [["A man, a plan, a canal: Panama"], True],
            [["race a car"], False],
            [[""], True],
            [[".,"], True],
            [["0P"], False],
        ],
        "def is_palindrome(s):\n    t = [c.lower() for c in s if c.isalnum()]\n    return t == t[::-1]\n",
        "O(n)",
        "O(n)",
    ),
    fn(
        "str-longest-unique",
        "arrays_strings",
        "Write a Python function length_of_longest_substring(s) that returns the length of the longest substring of s with no repeated characters.",
        "length_of_longest_substring",
        [[["abcabcbb"], 3], [["bbbbb"], 1], [["pwwkew"], 3], [[""], 0], [["abba"], 2]],
        "def length_of_longest_substring(s):\n    last, start, best = {}, 0, 0\n    for i, c in enumerate(s):\n        if c in last and last[c] >= start:\n            start = last[c] + 1\n        last[c] = i\n        best = max(best, i - start + 1)\n    return best\n",
        "O(n)",
        "O(k)",
    ),
    fn(
        "arr-max-subarray",
        "arrays_strings",
        "Write a Python function max_subarray(nums) that returns the largest sum of any non-empty contiguous subarray of the list nums.",
        "max_subarray",
        [
            [[[-2, 1, -3, 4, -1, 2, 1, -5, 4]], 6],
            [[[1]], 1],
            [[[-3, -1, -2]], -1],
            [[[5, 4, -1, 7, 8]], 23],
        ],
        "def max_subarray(nums):\n    best = cur = nums[0]\n    for n in nums[1:]:\n        cur = max(n, cur + n)\n        best = max(best, cur)\n    return best\n",
        "O(n)",
        "O(1)",
    ),
    fn(
        "map-group-anagrams",
        "hash_maps",
        "Write a Python function group_anagrams(words) that groups the words in the list that are anagrams of each other and returns a list of groups (lists). Group order and order inside a group do not matter.",
        "group_anagrams",
        [
            [
                [["eat", "tea", "tan", "ate", "nat", "bat"]],
                [["eat", "tea", "ate"], ["tan", "nat"], ["bat"]],
            ],
            [[[""]], [[""]]],
            [[["a"]], [["a"]]],
        ],
        "def group_anagrams(words):\n    groups = {}\n    for w in words:\n        groups.setdefault(''.join(sorted(w)), []).append(w)\n    return list(groups.values())\n",
        "O(n * k log k)",
        "O(n * k)",
        norm="groups",
    ),
    fn(
        "map-first-unique",
        "hash_maps",
        "Write a Python function first_unique_char(s) that returns the index of the first character of the string s that appears only once, or -1 if there is none.",
        "first_unique_char",
        [[["leetcode"], 0], [["loveleetcode"], 2], [["aabb"], -1], [[""], -1]],
        "def first_unique_char(s):\n    from collections import Counter\n    c = Counter(s)\n    for i, ch in enumerate(s):\n        if c[ch] == 1:\n            return i\n    return -1\n",
        "O(n)",
        "O(k)",
    ),
    fn(
        "sort-merge-intervals",
        "sorting_searching",
        "Write a Python function merge_intervals(intervals) that takes a list of [start, end] pairs and returns the list of merged overlapping intervals sorted by start. Intervals that only touch (for example [1,3] and [3,5]) count as overlapping.",
        "merge_intervals",
        [
            [[[[1, 3], [2, 6], [8, 10], [15, 18]]], [[1, 6], [8, 10], [15, 18]]],
            [[[[1, 4], [4, 5]]], [[1, 5]]],
            [[[]], []],
            [[[[5, 7], [1, 2]]], [[1, 2], [5, 7]]],
        ],
        "def merge_intervals(intervals):\n    out = []\n    for s, e in sorted(intervals):\n        if out and s <= out[-1][1]:\n            out[-1][1] = max(out[-1][1], e)\n        else:\n            out.append([s, e])\n    return out\n",
        "O(n log n)",
        "O(n)",
    ),
    fn(
        "search-rotated",
        "sorting_searching",
        "Write a Python function search_rotated(nums, target) that returns the index of target in nums, a sorted list of distinct integers that was rotated at an unknown pivot, or -1 if it is absent. It must run in O(log n) time.",
        "search_rotated",
        [
            [[[4, 5, 6, 7, 0, 1, 2], 0], 4],
            [[[4, 5, 6, 7, 0, 1, 2], 3], -1],
            [[[1], 0], -1],
            [[[1], 1], 0],
            [[[], 5], -1],
            [[[3, 1], 1], 1],
        ],
        "def search_rotated(nums, target):\n    lo, hi = 0, len(nums) - 1\n    while lo <= hi:\n        mid = (lo + hi) // 2\n        if nums[mid] == target:\n            return mid\n        if nums[lo] <= nums[mid]:\n            if nums[lo] <= target < nums[mid]:\n                hi = mid - 1\n            else:\n                lo = mid + 1\n        else:\n            if nums[mid] < target <= nums[hi]:\n                lo = mid + 1\n            else:\n                hi = mid - 1\n    return -1\n",
        "O(log n)",
        "O(1)",
    ),
    fn(
        "sort-kth-largest",
        "sorting_searching",
        "Write a Python function kth_largest(nums, k) that returns the k-th largest element of the list nums (k = 1 is the largest). Duplicates count separately.",
        "kth_largest",
        [[[[3, 2, 1, 5, 6, 4], 2], 5], [[[3, 2, 3, 1, 2, 4, 5, 5, 6], 4], 4], [[[1], 1], 1]],
        "def kth_largest(nums, k):\n    import heapq\n    return heapq.nlargest(k, nums)[-1]\n",
        "O(n log k)",
        "O(k)",
    ),
    fn(
        "rec-permutations",
        "recursion",
        "Write a Python function permutations(nums) that returns all permutations of a list of distinct integers, as a list of lists, in any order.",
        "permutations",
        [
            [[[1, 2, 3]], [[1, 2, 3], [1, 3, 2], [2, 1, 3], [2, 3, 1], [3, 1, 2], [3, 2, 1]]],
            [[[0, 1]], [[0, 1], [1, 0]]],
            [[[1]], [[1]]],
            [[[]], [[]]],
        ],
        "def permutations(nums):\n    if not nums:\n        return [[]]\n    out = []\n    for i, n in enumerate(nums):\n        for p in permutations(nums[:i] + nums[i + 1:]):\n            out.append([n] + p)\n    return out\n",
        "O(n * n!)",
        "O(n * n!)",
        norm="sorted",
    ),
    fn(
        "rec-flatten",
        "recursion",
        "Write a Python function flatten(nested) that takes a list that may contain integers and further lists nested to any depth and returns a flat list of the integers in their original left-to-right order.",
        "flatten",
        [
            [[[1, [2, [3, [4]], 5]]], [1, 2, 3, 4, 5]],
            [[[]], []],
            [[[[], [[]]]], []],
            [[[1, 2, 3]], [1, 2, 3]],
        ],
        "def flatten(nested):\n    out = []\n    for x in nested:\n        if isinstance(x, list):\n            out.extend(flatten(x))\n        else:\n            out.append(x)\n    return out\n",
        "O(n)",
        "O(d)",
    ),
    fn(
        "tree-max-depth",
        "trees_graphs",
        "A binary tree is given as nested tuples (value, left, right), where an empty subtree is None, and the empty tree is None. Write a Python function max_depth(root) that returns the number of nodes on the longest path from the root down to a leaf.",
        "max_depth",
        [
            [[None], 0],
            [[(1, None, None)], 1],
            [[(3, (9, None, None), (20, (15, None, None), (7, None, None)))], 3],
            [[(1, (2, (3, (4, None, None), None), None), None)], 4],
        ],
        "def max_depth(root):\n    if root is None:\n        return 0\n    return 1 + max(max_depth(root[1]), max_depth(root[2]))\n",
        "O(n)",
        "O(h)",
    ),
    fn(
        "graph-islands",
        "trees_graphs",
        "Write a Python function num_islands(grid) where grid is a list of strings made of '1' (land) and '0' (water). Return the number of islands: groups of land cells connected horizontally or vertically.",
        "num_islands",
        [
            [[["11000", "11000", "00100", "00011"]], 3],
            [[[]], 0],
            [[["000"]], 0],
            [[["111", "010", "111"]], 1],
        ],
        "def num_islands(grid):\n    seen, count = set(), 0\n    for r in range(len(grid)):\n        for c in range(len(grid[0])):\n            if grid[r][c] == '1' and (r, c) not in seen:\n                count += 1\n                stack = [(r, c)]\n                seen.add((r, c))\n                while stack:\n                    y, x = stack.pop()\n                    for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):\n                        ny, nx = y + dy, x + dx\n                        if 0 <= ny < len(grid) and 0 <= nx < len(grid[0]) and grid[ny][nx] == '1' and (ny, nx) not in seen:\n                            seen.add((ny, nx))\n                            stack.append((ny, nx))\n    return count\n",
        "O(r * c)",
        "O(r * c)",
    ),
    fn(
        "graph-shortest-path",
        "trees_graphs",
        "Write a Python function shortest_path_length(graph, start, end) where graph is a dict mapping each node to a list of its neighbours in an undirected, unweighted graph. Return the minimum number of edges from start to end, or -1 if end cannot be reached.",
        "shortest_path_length",
        [
            [[{"a": ["b", "c"], "b": ["a", "d"], "c": ["a", "d"], "d": ["b", "c"]}, "a", "d"], 2],
            [[{"a": ["b"], "b": ["a"], "c": []}, "a", "c"], -1],
            [[{"a": []}, "a", "a"], 0],
        ],
        "def shortest_path_length(graph, start, end):\n    from collections import deque\n    q, seen = deque([(start, 0)]), {start}\n    while q:\n        n, d = q.popleft()\n        if n == end:\n            return d\n        for m in graph.get(n, []):\n            if m not in seen:\n                seen.add(m)\n                q.append((m, d + 1))\n    return -1\n",
        "O(V + E)",
        "O(V)",
    ),
    fn(
        "dp-coin-change",
        "dynamic_programming",
        "Write a Python function coin_change(coins, amount) that returns the fewest coins needed to make exactly amount from an unlimited supply of the given coin values, or -1 if it cannot be made. coin_change(coins, 0) is 0.",
        "coin_change",
        [
            [[[1, 2, 5], 11], 3],
            [[[2], 3], -1],
            [[[1], 0], 0],
            [[[186, 419, 83, 408], 6249], 20],
            [[[3, 7], 11], -1],
        ],
        "def coin_change(coins, amount):\n    INF = amount + 1\n    dp = [0] + [INF] * amount\n    for a in range(1, amount + 1):\n        for c in coins:\n            if c <= a:\n                dp[a] = min(dp[a], dp[a - c] + 1)\n    return dp[amount] if dp[amount] != INF else -1\n",
        "O(amount * n)",
        "O(amount)",
    ),
    fn(
        "dp-lcs",
        "dynamic_programming",
        "Write a Python function lcs_length(a, b) that returns the length of the longest common subsequence of the strings a and b.",
        "lcs_length",
        [
            [["abcde", "ace"], 3],
            [["abc", "abc"], 3],
            [["abc", "def"], 0],
            [["", "abc"], 0],
            [["bsbininm", "jmjkbkjkv"], 1],
        ],
        "def lcs_length(a, b):\n    dp = [[0] * (len(b) + 1) for _ in range(len(a) + 1)]\n    for i in range(len(a)):\n        for j in range(len(b)):\n            dp[i + 1][j + 1] = dp[i][j] + 1 if a[i] == b[j] else max(dp[i][j + 1], dp[i + 1][j])\n    return dp[-1][-1]\n",
        "O(m * n)",
        "O(m * n)",
    ),
    fn(
        "dp-house-robber",
        "dynamic_programming",
        "Write a Python function rob(nums) where nums[i] is the money in house i along a street. You cannot take from two adjacent houses. Return the maximum total you can take. An empty street gives 0.",
        "rob",
        [[[[1, 2, 3, 1]], 4], [[[2, 7, 9, 3, 1]], 12], [[[]], 0], [[[5]], 5], [[[2, 1, 1, 2]], 4]],
        "def rob(nums):\n    a = b = 0\n    for n in nums:\n        a, b = b, max(b, a + n)\n    return b\n",
        "O(n)",
        "O(1)",
    ),
    other(
        "sql-second-highest",
        "sql",
        "Table employees(id, name, salary, dept). Write a SQL query that returns the second highest distinct salary as a single column named second_salary, or NULL if there is no second highest.",
        language="sql",
        kind="sql",
        sql_setup=EMP_SETUP,
        sql_expected=[[6000]],
        wants_complexity=False,
        wants_tests=False,
        time="n/a",
        space="n/a",
        reference="SELECT (SELECT DISTINCT salary FROM employees ORDER BY salary DESC LIMIT 1 OFFSET 1) AS second_salary;",
    ),
    other(
        "sql-dept-headcount",
        "sql",
        "Table employees(id, name, salary, dept). Write a SQL query that returns each department that has more than one employee, with its headcount as head_count, ordered by head_count descending and then dept ascending.",
        language="sql",
        kind="sql",
        sql_setup=EMP_SETUP,
        sql_expected=[["eng", 3], ["ops", 2]],
        wants_complexity=False,
        wants_tests=False,
        time="n/a",
        space="n/a",
        reference="SELECT dept, COUNT(*) AS head_count FROM employees GROUP BY dept HAVING COUNT(*) > 1 ORDER BY head_count DESC, dept ASC;",
    ),
    fn(
        "debug-find-max",
        "debugging",
        "This function should return the largest number in a non-empty list, but find_max([-3, -1, -2]) returns 0. Find the bug and fix it.\n\ndef find_max(nums):\n    biggest = 0\n    for n in nums:\n        if n > biggest:\n            biggest = n\n    return biggest",
        "find_max",
        [[[[-3, -1, -2]], -1], [[[4, 9, 2]], 9], [[[-7]], -7]],
        "def find_max(nums):\n    biggest = nums[0]\n    for n in nums:\n        if n > biggest:\n            biggest = n\n    return biggest\n",
        "O(n)",
        "O(1)",
    ),
    fn(
        "debug-mutable-default",
        "debugging",
        "This function should return a new list that holds only the item it was given, but calling add_item('a') and then add_item('b') returns ['a', 'b'] the second time. Explain why and fix it.\n\ndef add_item(item, items=[]):\n    items.append(item)\n    return items",
        "add_item",
        [[["a"], ["a"]], [["b"], ["b"]], [["c", ["x"]], ["x", "c"]]],
        "def add_item(item, items=None):\n    if items is None:\n        items = []\n    items.append(item)\n    return items\n",
        "O(1)",
        "O(1)",
    ),
    other(
        "complexity-nested-loop",
        "complexity",
        "What is the time and space complexity of this function, and why?\n\ndef f(n):\n    count = 0\n    for i in range(n):\n        j = 1\n        while j < n:\n            count += 1\n            j *= 2\n    return count",
        time="O(n log n)",
        space="O(1)",
        wants_code=False,
        wants_tests=False,
        must_mention=["log"],
    ),
    other(
        "edge-merge-lists",
        "edge_cases",
        "What edge cases would you test for a function that merges two sorted linked lists into one sorted list?",
        time="n/a",
        space="n/a",
        wants_code=False,
        wants_complexity=False,
        wants_tests=False,
        must_mention=["empty", "duplicate"],
    ),
    other(
        "incomplete-duplicates",
        "incomplete",
        "Write a function that finds the duplicates in a list.",
        time="n/a",
        space="n/a",
        wants_assumptions=True,
    ),
    other(
        "incomplete-cut-off",
        "incomplete",
        "Given an array of integers nums and an integer target, return the indices of the two numbers that add up to target.\n\nExample 1:\nInput: nums = [2,7,11,15], target = 9\nOutput: [0,1]\n\nConstraints:\n[cut off]",
        source="screenshot_extracted",
        time="O(n)",
        space="O(n)",
        wants_assumptions=True,
    ),
    fn(
        "screenshot-valid-parentheses",
        "screenshot_extracted",
        "Given a string s containing just the characters '(', ')', '{', '}', '[' and ']', determine if the input string is valid.\n\nAn input string is valid if open brackets are closed by the same type of bracket and in the correct order, and every close bracket has a matching open bracket.\n\nExample 1:\nInput: s = \"()[]{}\"\nOutput: true\n\nExample 2:\nInput: s = \"(]\"\nOutput: false\n\nConstraints:\n1 <= s.length <= 10000\n\ndef is_valid(s: str) -> bool:",
        "is_valid",
        [
            [["()[]{}"], True],
            [["(]"], False],
            [["([)]"], False],
            [["{[]}"], True],
            [["("], False],
            [[")("], False],
        ],
        "def is_valid(s):\n    pairs = {')': '(', ']': '[', '}': '{'}\n    stack = []\n    for c in s:\n        if c in pairs:\n            if not stack or stack.pop() != pairs[c]:\n                return False\n        else:\n            stack.append(c)\n    return not stack\n",
        "O(n)",
        "O(n)",
        source="screenshot_extracted",
    ),
    other(
        "lang-java-reverse-list",
        "language",
        "In Java, reverse a singly linked list and return the new head.",
        language="java",
        time="O(n)",
        space="O(1)",
    ),
    other(
        "lang-js-debounce",
        "language",
        "In JavaScript, write a debounce(fn, delay) function that delays calling fn until delay milliseconds have passed since the last call.",
        language="javascript",
        time="O(1)",
        space="O(1)",
        wants_complexity=False,
    ),
]

# Other accepted ways to write the reference time complexity: an equally standard algorithm with a
# different bound, and other names for the same dimensions. Added after the first scoring run showed
# notation-only mismatches; both arms are scored with the same list. Whether a stated complexity is
# right for the code actually written still needs a human (docs/CODING_ASSISTANCE_EVALUATION.md).
TIME_ALTERNATIVES: dict[str, list[str]] = {
    "sort-kth-largest": ["O(n log n)"],  # sort and index: standard, simpler, slower bound
    "graph-islands": ["O(m * n)", "O(rows * cols)", "O(R * C)", "O(V)"],
    "dp-coin-change": ["O(amount * len(coins))", "O(a * c)", "O(S * n)"],
    "dp-lcs": ["O(len(a) * len(b))", "O(n * m)"],
    "map-group-anagrams": ["O(n * k)", "O(N * K log K)", "O(nk log k)"],
    "rec-permutations": ["O(n!)", "O(n! * n)"],
}
for _row in ROWS:
    _row["time_alt"] = TIME_ALTERNATIVES.get(_row["id"], [])


def main() -> None:
    ids = [r["id"] for r in ROWS]
    if len(ids) != len(set(ids)):
        raise SystemExit("duplicate id")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", encoding="utf-8", newline="\n") as handle:
        for row in ROWS:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(f"wrote {len(ROWS)} coding problems to {OUT}")


if __name__ == "__main__":
    main()
