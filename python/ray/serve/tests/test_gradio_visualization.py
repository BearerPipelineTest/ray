import pytest
from collections import defaultdict
import asyncio
import aiohttp
import random

from ray.serve.experimental.gradio_visualize_graph import GraphVisualizer

from ray import serve
from ray.dag import InputNode
from ray.serve.drivers import DAGDriver


@pytest.fixture
def graph1():
    @serve.deployment
    def f(x):
        return x

    @serve.deployment
    class Model:
        def __init__(self, _):
            pass

        def run(self, x):
            return x

    with InputNode() as user_input:
        input_nodes = (user_input[0], user_input["key"])
        f_node = f.bind(input_nodes[0])
        m = Model.bind(f_node)
        dag = m.run.bind(input_nodes[1])

    yield input_nodes, f_node, m, dag


@pytest.fixture
def graph2():
    @serve.deployment
    def f(_, x=0):
        return x

    with InputNode() as user_input:
        input_node = user_input[0]
        f_node = f.bind(input_node)
        dag = f.bind(f_node, input_node)

    yield input_node, f_node, dag


@pytest.fixture
def graph3():
    @serve.deployment
    class Base:
        def __init__(self, weight):
            self.weight = weight

        def eval(self, input1, input2) -> int:
            return (input1 + input2) * self.weight

    @serve.deployment
    class Model:
        def __init__(self, weight):
            self.weight = weight

        def forward(self, input) -> int:
            return input * self.weight

    @serve.deployment
    def combine(x, y, z):
        return x + y + z

    with InputNode() as user_input:
        input_nodes = (user_input[0], user_input[1], user_input[2])

        b = Base.bind(1)
        m1 = Model.bind(1)
        m2 = Model.bind(1)
        l_output = b.eval.bind(input_nodes[0], input_nodes[1])
        m1_output = m1.forward.bind(l_output)
        m2_output = m2.forward.bind(input_nodes[2])
        dag = combine.bind(m1_output, m2_output, l_output)

    yield input_nodes, b, m1, m2, l_output, m1_output, m2_output, dag


@pytest.mark.asyncio
async def test_execute_cached_object_ref(graph1):
    """Tests DAGNode.get_object_ref_from_last_execute() correctly returns object refs
    to the submitted tasks after DAGNode.execute() is run.
    """
    (_, f_node, _, dag) = graph1

    dag.execute(1, key=2, _ray_cache_refs=True)
    cache = await dag.get_object_refs_from_last_execute()
    assert await cache[f_node.get_stable_uuid()] == 1
    assert await cache[dag.get_stable_uuid()] == 2


class TestGraphDFS:
    def test_graph_dfs_for_depths1(self, graph1):
        """Tests that GraphVisualizer._fetch_depths, when passed into
        DAGNode.apply_recursive, correctly retrieves the depths of each node.
        """
        (input_nodes, f_node, _, dag) = graph1

        visualizer = GraphVisualizer()
        depths = defaultdict(lambda: 0)
        dag.apply_recursive(lambda node: visualizer._fetch_depths(node, depths))

        assert (
            depths[input_nodes[0].get_stable_uuid()] == 1
            and depths[input_nodes[1].get_stable_uuid()] == 1
            and depths[f_node.get_stable_uuid()] == 2
            and depths[dag.get_stable_uuid()] == 4
        )

    def test_graph_dfs_for_depths2(self, graph2):
        """Tests that GraphVisualizer._fetch_depths, when passed into
        DAGNode.apply_recursive, correctly retrieves the depths of each node.
        """

        (input_node, f_node, dag) = graph2

        visualizer = GraphVisualizer()
        depths = defaultdict(lambda: 0)
        dag.apply_recursive(lambda node: visualizer._fetch_depths(node, depths))

        assert (
            depths[input_node.get_stable_uuid()] == 1
            and depths[f_node.get_stable_uuid()] == 2
            and depths[dag.get_stable_uuid()] == 3
        )

    def test_graph_dfs_for_depths3(self, graph3):
        """Tests that GraphVisualizer._fetch_depths, when passed into
        DAGNode.apply_recursive, correctly retrieves the depths of each node.
        """

        (input_nodes, _, _, _, l_output, m1_output, m2_output, dag) = graph3

        visualizer = GraphVisualizer()
        depths = defaultdict(lambda: 0)
        dag.apply_recursive(lambda node: visualizer._fetch_depths(node, depths))

        assert (depths[input_node.get_stable_uuid()] == 1 for input_node in input_nodes)
        assert (
            depths[l_output.get_stable_uuid()] == 2
            and depths[m2_output.get_stable_uuid()] == 2
        )
        assert depths[m1_output.get_stable_uuid()] == 3
        assert depths[dag.get_stable_uuid()] == 4


@pytest.mark.asyncio
async def test_get_result_correctness(graph1):
    """Tests correctness: that after running _send_request(), _get_result() in
    GraphVisualizer correctly returns object refs to the submitted tasks.
    """
    (_, _, _, dag) = graph1

    handle = serve.run(DAGDriver.bind(dag))
    visualizer = GraphVisualizer()
    visualizer.visualize_with_gradio(handle, _launch=False)

    await visualizer._send_request(random.randint(0, 100), 1, 2)
    values = await asyncio.gather(
        *[
            (visualizer._get_result(node.get_stable_uuid()))
            for node in visualizer.node_to_block
        ]
    )
    assert {1, 2} <= set(values)


@pytest.mark.asyncio
async def test_gradio_visualization_e2e(graph1):
    """Tests the E2E process of launching the Gradio app and submitting input.
    Simulates clicking the submit button by sending asynchronous HTTP requests.
    """
    (_, _, _, dag) = graph1

    handle = serve.run(DAGDriver.bind(dag))
    visualizer = GraphVisualizer()
    (_, url, _) = visualizer.visualize_with_gradio(handle, _launch=True, _block=False)

    async with aiohttp.ClientSession() as session:

        async def fetch(data, fn_index):
            async with session.post(
                f"{url.strip('/')}/api/predict/",
                json={
                    "session_hash": "random_hash",
                    "data": data,
                    "fn_index": fn_index,
                },
            ) as resp:
                return (await resp.json())["data"]

        await fetch(
            [random.randint(0, 100), 1, 2], 0
        )  # sends request to dag with input (1,2)
        values = await asyncio.gather(
            fetch([], 1),  # fetches return value for one of the nodes
            fetch([], 2),  # fetches return value for the other node
        )

    assert [1] in values and [2] in values


if __name__ == "__main__":
    import sys

    sys.exit(pytest.main(["-v", "-s", __file__]))
