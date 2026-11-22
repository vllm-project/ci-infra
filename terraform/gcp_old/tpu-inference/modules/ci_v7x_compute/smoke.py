import functools
import json
import os
import socket
import jax
import jax.numpy as jnp
import numpy as np
hosts=int(os.environ['TPU_HOST_COUNT'])
if hosts > 1:
    jax.distributed.initialize(coordinator_address=os.environ['JAX_COORDINATOR_ADDRESS'], num_processes=hosts, process_id=int(os.environ['TPU_PROCESS_ID']), initialization_timeout=180)
assert jax.process_count() == hosts, jax.process_count()
assert jax.device_count() == 8 * hosts, jax.device_count()
assert jax.local_device_count() == 8, jax.local_device_count()
x = jnp.ones((128, 128), dtype=jnp.float32)
y = jax.jit(lambda a: a @ a)(x).block_until_ready()
np.testing.assert_allclose(np.asarray(y), 128.0)
@functools.partial(jax.pmap, axis_name='devices')
def global_sum(x):
    return jax.lax.psum(x, 'devices')
result = global_sum(jnp.ones((jax.local_device_count(),))).block_until_ready()
np.testing.assert_allclose(np.asarray(result), float(8 * hosts))
print(json.dumps({'host':socket.gethostname(),'process':jax.process_index(),'processes':jax.process_count(),'local_devices':jax.local_device_count(),'global_devices':jax.device_count(),'matmul':'passed','all_reduce':'passed'}),flush=True)
if hosts > 1:
    jax.distributed.shutdown()
