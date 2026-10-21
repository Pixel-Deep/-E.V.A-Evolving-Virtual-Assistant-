import unittest
from eva.coding.algorithms import binary_search, merge_sort, breadth_first_search, dijkstra

class AlgorithmTests(unittest.TestCase):
    def test_binary_search_boundaries_and_absence(self):
        self.assertEqual(binary_search([], 1), -1)
        self.assertEqual(binary_search([4], 4), 0)
        self.assertEqual(binary_search([1, 2, 2, 4], 2), 1)
        self.assertEqual(binary_search([1, 2, 4], 3), -1)

    def test_merge_sort_empty_duplicates_and_stability(self):
        self.assertEqual(merge_sort([]), [])
        self.assertEqual(merge_sort([3, -1, 2, 2]), [-1, 2, 2, 3])
        data = [(1, 'a'), (0, 'x'), (1, 'b')]
        self.assertEqual(merge_sort(data, key=lambda item: item[0]), [(0, 'x'), (1, 'a'), (1, 'b')])

    def test_bfs_cycle_and_disconnected_graph(self):
        graph = {1: [2, 3], 2: [1, 4], 3: [1], 4: [2], 9: []}
        self.assertEqual(breadth_first_search(graph, 1), [1, 2, 3, 4])
        self.assertEqual(breadth_first_search(graph, 9), [9])

    def test_dijkstra_shortest_paths_and_negative_weight_rejected(self):
        graph = {'a': [('b', 4), ('c', 1)], 'c': [('b', 2)], 'b': [], 'z': []}
        self.assertEqual(dijkstra(graph, 'a'), {'a': 0.0, 'c': 1.0, 'b': 3.0})
        with self.assertRaises(ValueError): dijkstra({'a': [('b', -1)]}, 'a')

if __name__ == '__main__': unittest.main()
