# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.

import sys
import pytest

import tvm
import tvm.testing
from tvm import tir
from tvm.script import tir as T

import numpy as np


def opt_gemm_normalize():
    @tvm.script.ir_module
    class Module:
        @T.prim_func
        def mmult(A: T.handle, B: T.handle, C: T.handle) -> None:
            # function attr dict
            T.func_attr({"global_symbol": "mmult", "tir.noalias": True})
            # buffer definition
            C_global = T.buffer_decl([1024, 1024], elem_offset=0, align=64, offset_factor=1)
            packedB = T.buffer_decl([32, 1024, 32], elem_offset=0, align=64, offset_factor=1)
            A_1 = T.match_buffer(A, [1024, 1024], elem_offset=0, align=64, offset_factor=1)
            B_1 = T.match_buffer(B, [1024, 1024], elem_offset=0, align=64, offset_factor=1)
            C_1 = T.match_buffer(C, [1024, 1024], elem_offset=0, align=64, offset_factor=1)
            # body
            T.realize(packedB[0:32, 0:1024, 0:32], "")
            for x in T.parallel(0, 32):
                for y in T.serial(0, 1024):
                    for z in T.vectorized(0, 32):
                        packedB[x, y, z] = B_1[y, ((x * 32) + z)]
            T.realize(C_1[0:1024, 0:1024], "")
            for x_outer in T.parallel(0, 32):
                for y_outer in T.serial(0, 32):
                    T.realize(
                        C_global[
                            (x_outer * 32) : ((x_outer * 32) + 32),
                            (y_outer * 32) : ((y_outer * 32) + 32),
                        ],
                        "global",
                    )
                    for x_c_init in T.serial(0, 32):
                        for y_c_init in T.vectorized(0, 32):
                            C_global[
                                (x_c_init + (x_outer * 32)), (y_c_init + (y_outer * 32))
                            ] = T.float32(0)
                    for k_outer in T.serial(0, 256):
                        for x_c in T.serial(0, 32):
                            for k_inner in T.unroll(0, 4):
                                for y_c in T.vectorized(0, 32):
                                    C_global[
                                        (x_c + (x_outer * 32)), (y_c + (y_outer * 32))
                                    ] = C_global[(x_c + (x_outer * 32)), (y_c + (y_outer * 32))] + (
                                        A_1[(x_c + (x_outer * 32)), (k_inner + (k_outer * 4))]
                                        * packedB[
                                            T.floordiv((y_c + (y_outer * 32)), 32),
                                            (k_inner + (k_outer * 4)),
                                            T.floormod((y_c + (y_outer * 32)), 32),
                                        ]
                                    )
                    for x_inner in T.serial(0, 32):
                        for y_inner in T.serial(0, 32):
                            C_1[(x_inner + (x_outer * 32)), (y_inner + (y_outer * 32))] = C_global[
                                (x_inner + (x_outer * 32)), (y_inner + (y_outer * 32))
                            ]

    return Module


def opt_gemm_lower():
    @tvm.script.ir_module
    class Module:
        @T.prim_func
        def mmult(A: T.handle, B: T.handle, C: T.handle) -> None:
            # function attr dict
            T.func_attr({"global_symbol": "mmult", "tir.noalias": True})
            A_1 = T.match_buffer(A, [1024 * 1024], elem_offset=0, align=64, offset_factor=1)
            B_1 = T.match_buffer(B, [1024, 1024], elem_offset=0, align=64, offset_factor=1)
            C_1 = T.match_buffer(C, [1024 * 1024], elem_offset=0, align=64, offset_factor=1)
            # body
            packedB = T.allocate([32768], "float32", "global")
            for x in T.parallel(0, 32):
                for y in T.serial(0, 1024):
                    packedB[T.ramp(((x * 32768) + (y * 32)), 1, 32)] = B_1[y, T.ramp(x * 32, 1, 32)]
            for x_outer in T.parallel(0, 32):
                C_global = T.allocate([1024], "float32", "global")
                for y_outer in T.serial(0, 32):
                    for x_c_init in T.serial(0, 32):
                        C_global[T.ramp((x_c_init * 32), 1, 32)] = T.broadcast(T.float32(0), 32)
                    for k_outer in T.serial(0, 256):
                        for x_c in T.serial(0, 32):
                            C_global[T.ramp((x_c * 32), 1, 32)] = C_global[
                                T.ramp((x_c * 32), 1, 32)
                            ] + (
                                T.broadcast(
                                    A_1[
                                        (((x_outer * 32768) + (x_c * 1024)) + (k_outer * 4)),
                                    ],
                                    32,
                                )
                                * packedB[T.ramp(((y_outer * 32768) + (k_outer * 128)), 1, 32)]
                            )
                            C_global[T.ramp((x_c * 32), 1, 32)] = C_global[
                                T.ramp((x_c * 32), 1, 32)
                            ] + (
                                T.broadcast(
                                    A_1[
                                        ((((x_outer * 32768) + (x_c * 1024)) + (k_outer * 4)) + 1),
                                    ],
                                    32,
                                )
                                * packedB[
                                    T.ramp((((y_outer * 32768) + (k_outer * 128)) + 32), 1, 32)
                                ]
                            )
                            C_global[T.ramp((x_c * 32), 1, 32)] = C_global[
                                T.ramp((x_c * 32), 1, 32)
                            ] + (
                                T.broadcast(
                                    A_1[
                                        ((((x_outer * 32768) + (x_c * 1024)) + (k_outer * 4)) + 2),
                                    ],
                                    32,
                                )
                                * packedB[
                                    T.ramp((((y_outer * 32768) + (k_outer * 128)) + 64), 1, 32)
                                ]
                            )
                            C_global[T.ramp((x_c * 32), 1, 32)] = C_global[
                                T.ramp((x_c * 32), 1, 32)
                            ] + (
                                T.broadcast(
                                    A_1[
                                        ((((x_outer * 32768) + (x_c * 1024)) + (k_outer * 4)) + 3),
                                    ],
                                    32,
                                )
                                * packedB[
                                    T.ramp((((y_outer * 32768) + (k_outer * 128)) + 96), 1, 32)
                                ]
                            )
                    for x_inner in T.serial(0, 32):
                        for y_inner in T.serial(0, 32):
                            C_1[
                                (
                                    (((x_outer * 32768) + (x_inner * 1024)) + (y_outer * 32))
                                    + y_inner
                                )
                            ] = C_global[((x_inner * 32) + y_inner)]

    return Module


def opt_gemm_mod_host():
    @tvm.script.ir_module
    class Module:
        @T.prim_func
        def mmult(
            args: T.handle,
            arg_type_ids: T.handle,
            num_args: T.int32,
            out_ret_value: T.handle,
            out_ret_tcode: T.handle,
        ) -> T.int32:
            # function attr dict
            T.func_attr(
                {
                    "tir.noalias": True,
                    "global_symbol": "mmult",
                    "tir.is_entry_func": True,
                    "calling_conv": 1,
                }
            )
            # buffer definition
            buf_type_ids = T.match_buffer(arg_type_ids, [3], dtype="int32")

            packedB = T.buffer_decl([32768], dtype="float32")
            C_global = T.buffer_decl([1024], dtype="float32")
            # var definition
            # C_global = T.buffer_var("float32", "global")
            # packedB = T.buffer_var("float32", "global")
            # body
            assert num_args == 3, "mmult: num_args should be 3"
            arg0: T.handle = T.tvm_struct_get(args, 0, 12, dtype="handle")
            arg0_code: T.int32 = buf_type_ids[0]
            arg1: T.handle = T.tvm_struct_get(args, 1, 12, dtype="handle")
            arg1_code: T.int32 = buf_type_ids[1]
            arg2: T.handle = T.tvm_struct_get(args, 2, 12, dtype="handle")
            arg2_code: T.int32 = buf_type_ids[2]

            A_data: T.Ptr[T.int32] = T.tvm_struct_get(arg0, 0, 1, dtype="handle")
            T.attr(A_data, "storage_alignment", 128)
            A = T.buffer_decl([1024 * 1024], dtype="int32", data=A_data)
            buf0_shape_data: T.Ptr[T.int32] = T.tvm_struct_get(arg0, 0, 2, dtype="handle")
            buf0_shape = T.buffer_decl([2], dtype="int32", data=buf0_shape_data)
            buf0_strides_data: T.Ptr[T.int32] = T.tvm_struct_get(arg0, 0, 3, dtype="handle")
            buf0_strides = T.buffer_decl([2], dtype="int32", data=buf0_strides_data)

            dev_id: T.int32 = T.tvm_struct_get(arg0, 0, 9, dtype="int32")

            B_data: T.Ptr[T.int32] = T.tvm_struct_get(arg1, 0, 1, dtype="handle")
            T.attr(B_data, "storage_alignment", 128)
            B = T.buffer_decl([1024 * 1024], dtype="int32", data=B_data)
            buf1_shape_data: T.Ptr[T.int32] = T.tvm_struct_get(arg1, 0, 2, dtype="handle")
            buf1_shape = T.buffer_decl([2], dtype="int32", data=buf1_shape_data)
            buf1_strides_data: T.Ptr[T.int32] = T.tvm_struct_get(arg1, 0, 3, dtype="handle")
            buf1_strides = T.buffer_decl([2], dtype="int32", data=buf1_strides_data)

            C_data: T.Ptr[T.int32] = T.tvm_struct_get(arg2, 0, 1, dtype="handle")
            T.attr(C_data, "storage_alignment", 128)
            C = T.buffer_decl([1024 * 1024], dtype="int32", data=C_data)
            buf2_shape_data: T.Ptr[T.int32] = T.tvm_struct_get(arg2, 0, 2, dtype="handle")
            buf2_shape = T.buffer_decl([2], dtype="int32", data=buf2_shape_data)
            buf2_strides_data: T.Ptr[T.int32] = T.tvm_struct_get(arg2, 0, 3, dtype="handle")
            buf2_strides = T.buffer_decl([2], dtype="int32", data=buf2_strides_data)

            assert (((arg0_code == 3) or (arg0_code == 13)) or (arg0_code == 7)) or (
                arg0_code == 4
            ), "mmult: Expect arg[0] to be pointer"
            assert (((arg1_code == 3) or (arg1_code == 13)) or (arg1_code == 7)) or (
                arg1_code == 4
            ), "mmult: Expect arg[1] to be pointer"
            assert (((arg2_code == 3) or (arg2_code == 13)) or (arg2_code == 7)) or (
                arg2_code == 4
            ), "mmult: Expect arg[2] to be pointer"
            assert 2 == T.tvm_struct_get(
                arg0, 0, 4, dtype="int32"
            ), "arg0.ndim is expected to equal 2"
            assert 2 == T.tvm_struct_get(
                arg0, 0, 4, dtype="int32"
            ), "arg0.ndim is expected to equal 2"
            assert (
                (T.tvm_struct_get(arg0, 0, 5, dtype="uint8") == T.uint8(2))
                and (T.tvm_struct_get(arg0, 0, 6, dtype="uint8") == T.uint8(32))
            ) and (
                T.tvm_struct_get(arg0, 0, 7, dtype="uint16") == T.uint16(1)
            ), "arg0.dtype is expected to be float32"
            assert 1024 == T.cast(
                buf0_shape[0], "int32"
            ), "Argument arg0.shape[0] has an unsatisfied constraint"
            assert 1024 == T.cast(
                buf0_shape[1], "int32"
            ), "Argument arg0.shape[1] has an unsatisfied constraint"
            if not (T.isnullptr(buf0_strides.data, dtype="bool")):
                assert (1 == T.cast(buf0_strides[1], "int32")) and (
                    1024 == T.cast(buf0_strides[0], "int32")
                ), "arg0.strides: expected to be compact array"
                T.evaluate(0)
            assert T.uint64(0) == T.tvm_struct_get(
                arg0, 0, 8, dtype="uint64"
            ), "Argument arg0.byte_offset has an unsatisfied constraint"
            assert 1 == T.tvm_struct_get(
                arg0, 0, 10, dtype="int32"
            ), "Argument arg0.device_type has an unsatisfied constraint"
            assert 2 == T.tvm_struct_get(
                arg1, 0, 4, dtype="int32"
            ), "arg1.ndim is expected to equal 2"
            assert 2 == T.tvm_struct_get(
                arg1, 0, 4, dtype="int32"
            ), "arg1.ndim is expected to equal 2"
            assert (
                (T.tvm_struct_get(arg1, 0, 5, dtype="uint8") == T.uint8(2))
                and (T.tvm_struct_get(arg1, 0, 6, dtype="uint8") == T.uint8(32))
            ) and (
                T.tvm_struct_get(arg1, 0, 7, dtype="uint16") == T.uint16(1)
            ), "arg1.dtype is expected to be float32"
            assert 1024 == T.cast(
                buf1_shape[0], "int32"
            ), "Argument arg1.shape[0] has an unsatisfied constraint"
            assert 1024 == T.cast(
                buf1_shape[1], "int32"
            ), "Argument arg1.shape[1] has an unsatisfied constraint"
            if not (T.isnullptr(buf1_strides.data, dtype="bool")):
                assert (1 == T.cast(buf1_strides[1], "int32")) and (
                    1024 == T.cast(buf1_strides[0], "int32")
                ), "arg1.strides: expected to be compact array"
                T.evaluate(0)
            assert T.uint64(0) == T.tvm_struct_get(
                arg1, 0, 8, dtype="uint64"
            ), "Argument arg1.byte_offset has an unsatisfied constraint"
            assert 1 == T.tvm_struct_get(
                arg1, 0, 10, dtype="int32"
            ), "Argument arg1.device_type has an unsatisfied constraint"
            assert dev_id == T.tvm_struct_get(
                arg1, 0, 9, dtype="int32"
            ), "Argument arg1.device_id has an unsatisfied constraint"
            assert 2 == T.tvm_struct_get(
                arg2, 0, 4, dtype="int32"
            ), "arg2.ndim is expected to equal 2"
            assert 2 == T.tvm_struct_get(
                arg2, 0, 4, dtype="int32"
            ), "arg2.ndim is expected to equal 2"
            assert (
                (T.tvm_struct_get(arg2, 0, 5, dtype="uint8") == T.uint8(2))
                and (T.tvm_struct_get(arg2, 0, 6, dtype="uint8") == T.uint8(32))
            ) and (
                T.tvm_struct_get(arg2, 0, 7, dtype="uint16") == T.uint16(1)
            ), "arg2.dtype is expected to be float32"
            assert 1024 == T.cast(
                buf2_shape[0], "int32"
            ), "Argument arg2.shape[0] has an unsatisfied constraint"
            assert 1024 == T.cast(
                buf2_shape[1], "int32"
            ), "Argument arg2.shape[1] has an unsatisfied constraint"
            if not (T.isnullptr(buf2_strides.data, dtype="bool")):
                assert (1 == T.cast(buf2_strides[1], "int32")) and (
                    1024 == T.cast(buf2_strides[0], "int32")
                ), "arg2.strides: expected to be compact array"
                T.evaluate(0)
            assert T.uint64(0) == T.tvm_struct_get(
                arg2, 0, 8, dtype="uint64"
            ), "Argument arg2.byte_offset has an unsatisfied constraint"
            assert 1 == T.tvm_struct_get(
                arg2, 0, 10, dtype="int32"
            ), "Argument arg2.device_type has an unsatisfied constraint"
            assert dev_id == T.tvm_struct_get(
                arg2, 0, 9, dtype="int32"
            ), "Argument arg2.device_id has an unsatisfied constraint"
            T.attr(0, "compute_scope", "mmult_compute_")
            T.attr(packedB.data, "storage_scope", "global")
            T.attr(packedB.data, "storage_alignment", 128)
            with T.let(
                packedB.data,
                T.TVMBackendAllocWorkspace(1, dev_id, T.uint64(4194304), 2, 32, dtype="handle"),
            ):
                if T.isnullptr(packedB.data, dtype="bool"):
                    T.evaluate(T.tvm_throw_last_error(dtype="int32"))
                for x in T.parallel(0, 32):
                    for y in T.serial(0, 1024):
                        packedB[T.ramp(((x * 32768) + (y * 32)), 1, 32)] = B[
                            T.ramp(((y * 1024) + (x * 32)), 1, 32)
                        ]
                for x_outer in T.parallel(0, 32):
                    T.attr(C_global.data, "storage_scope", "global")
                    T.attr(C_global.data, "storage_alignment", 128)
                    with T.let(
                        C_global.data,
                        T.TVMBackendAllocWorkspace(
                            1, dev_id, T.uint64(4096), 2, 32, dtype="handle"
                        ),
                    ):
                        if T.isnullptr(C_global.data, dtype="bool"):
                            T.evaluate(T.tvm_throw_last_error(dtype="int32"))
                        for y_outer in T.serial(0, 32):
                            for x_c_init in T.serial(0, 32):
                                C_global[T.ramp((x_c_init * 32), 1, 32)] = T.broadcast(
                                    T.float32(0), 32
                                )
                            for k_outer in T.serial(0, 256):
                                for x_c in T.serial(0, 32):
                                    C_global[T.ramp((x_c * 32), 1, 32)] = T.call_llvm_pure_intrin(
                                        T.uint32(97),
                                        T.uint32(3),
                                        T.broadcast(
                                            A[
                                                (
                                                    ((x_outer * 32768) + (x_c * 1024))
                                                    + (k_outer * 4)
                                                ),
                                            ],
                                            32,
                                        ),
                                        packedB[
                                            T.ramp(((y_outer * 32768) + (k_outer * 128)), 1, 32)
                                        ],
                                        C_global[T.ramp((x_c * 32), 1, 32)],
                                        dtype="float32x32",
                                    )
                                    C_global[T.ramp((x_c * 32), 1, 32)] = T.call_llvm_pure_intrin(
                                        T.uint32(97),
                                        T.uint32(3),
                                        T.broadcast(
                                            A[
                                                (
                                                    (
                                                        ((x_outer * 32768) + (x_c * 1024))
                                                        + (k_outer * 4)
                                                    )
                                                    + 1
                                                ),
                                            ],
                                            32,
                                        ),
                                        packedB[
                                            T.ramp(
                                                (((y_outer * 32768) + (k_outer * 128)) + 32), 1, 32
                                            )
                                        ],
                                        C_global[T.ramp((x_c * 32), 1, 32)],
                                        dtype="float32x32",
                                    )
                                    C_global[T.ramp((x_c * 32), 1, 32)] = T.call_llvm_pure_intrin(
                                        T.uint32(97),
                                        T.uint32(3),
                                        T.broadcast(
                                            A[
                                                (
                                                    (
                                                        ((x_outer * 32768) + (x_c * 1024))
                                                        + (k_outer * 4)
                                                    )
                                                    + 2
                                                ),
                                            ],
                                            32,
                                        ),
                                        packedB[
                                            T.ramp(
                                                (((y_outer * 32768) + (k_outer * 128)) + 64), 1, 32
                                            )
                                        ],
                                        C_global[T.ramp((x_c * 32), 1, 32)],
                                        dtype="float32x32",
                                    )
                                    C_global[T.ramp((x_c * 32), 1, 32)] = T.call_llvm_pure_intrin(
                                        T.uint32(97),
                                        T.uint32(3),
                                        T.broadcast(
                                            A[
                                                (
                                                    (
                                                        ((x_outer * 32768) + (x_c * 1024))
                                                        + (k_outer * 4)
                                                    )
                                                    + 3
                                                ),
                                            ],
                                            32,
                                        ),
                                        packedB[
                                            T.ramp(
                                                (((y_outer * 32768) + (k_outer * 128)) + 96), 1, 32
                                            )
                                        ],
                                        C_global[T.ramp((x_c * 32), 1, 32)],
                                        dtype="float32x32",
                                    )
                            for x_inner in T.serial(0, 32):
                                for y_inner in T.serial(0, 32):
                                    C[
                                        (
                                            (
                                                ((x_outer * 32768) + (x_inner * 1024))
                                                + (y_outer * 32)
                                            )
                                            + y_inner
                                        )
                                    ] = C_global[((x_inner * 32) + y_inner)]
                    if T.TVMBackendFreeWorkspace(1, dev_id, C_global.data, dtype="int32") != 0:
                        T.evaluate(T.tvm_throw_last_error(dtype="int32"))
            if T.TVMBackendFreeWorkspace(1, dev_id, packedB.data, dtype="int32") != 0:
                T.evaluate(T.tvm_throw_last_error(dtype="int32"))

    return Module


def opt_conv_tensorcore_normalize():
    @T.prim_func
    def func(A: T.handle, W: T.handle, Conv: T.handle) -> None:
        # function attr dict
        T.func_attr({"global_symbol": "default_function", "tir.noalias": True})
        # var definition
        bx = T.env_thread("blockIdx.x")
        by = T.env_thread("blockIdx.y")
        bz = T.env_thread("blockIdx.z")
        tx = T.env_thread("threadIdx.x")
        ty = T.env_thread("threadIdx.y")
        tz = T.env_thread("threadIdx.z")
        # buffer definition
        Apad_shared = T.buffer_decl(
            [16, 16, 16, 16, 16, 16], dtype="float16", elem_offset=0, align=64, offset_factor=1
        )
        Apad_shared_wmma_matrix_a = T.buffer_decl(
            [16, 16, 16, 16, 16, 16], dtype="float16", elem_offset=0, align=64, offset_factor=1
        )
        BA = T.buffer_decl(
            [16, 16], dtype="float16", scope="wmma.matrix_a", align=32, offset_factor=256
        )
        BB = T.buffer_decl(
            [16, 16], dtype="float16", scope="wmma.matrix_b", align=32, offset_factor=256
        )
        BC = T.buffer_decl([16, 16], scope="wmma.accumulator", align=32, offset_factor=256)
        Conv_wmma_accumulator = T.buffer_decl(
            [16, 14, 14, 32, 16, 16], elem_offset=0, align=64, offset_factor=1
        )
        W_shared = T.buffer_decl(
            [3, 3, 16, 32, 16, 16], dtype="float16", elem_offset=0, align=64, offset_factor=1
        )
        W_shared_wmma_matrix_b = T.buffer_decl(
            [3, 3, 16, 32, 16, 16], dtype="float16", elem_offset=0, align=64, offset_factor=1
        )
        buffer = T.buffer_decl(
            [16, 16], dtype="float16", scope="shared", align=32, offset_factor=256
        )
        buffer_1 = T.buffer_decl(
            [16, 16], dtype="float16", scope="wmma.matrix_a", align=32, offset_factor=256
        )
        buffer_2 = T.buffer_decl(
            [16, 16], dtype="float16", scope="shared", align=32, offset_factor=256
        )
        buffer_3 = T.buffer_decl(
            [16, 16], dtype="float16", scope="wmma.matrix_b", align=32, offset_factor=256
        )
        buffer_4 = T.buffer_decl([16, 16], scope="wmma.accumulator", align=32, offset_factor=256)
        buffer_5 = T.buffer_decl([16, 16], align=32, offset_factor=256)
        A_1 = T.match_buffer(
            A, [16, 14, 14, 16, 16, 16], dtype="float16", elem_offset=0, align=64, offset_factor=1
        )
        W_1 = T.match_buffer(
            W, [3, 3, 16, 32, 16, 16], dtype="float16", elem_offset=0, align=64, offset_factor=1
        )
        Conv_1 = T.match_buffer(
            Conv, [16, 14, 14, 32, 16, 16], elem_offset=0, align=64, offset_factor=1
        )
        # body
        T.realize(Conv_1[0:16, 0:14, 0:14, 0:32, 0:16, 0:16], "")
        T.launch_thread(bz, 196)
        T.launch_thread(bx, 2)
        T.launch_thread(by, 4)
        T.launch_thread(ty, 4)
        T.launch_thread(tz, 2)
        T.realize(
            Conv_wmma_accumulator[
                ((bx * 8) + (ty * 2)) : (((bx * 8) + (ty * 2)) + 2),
                T.floordiv(bz, 14) : (T.floordiv(bz, 14) + 1),
                T.floormod(bz, 14) : (T.floormod(bz, 14) + 1),
                ((by * 8) + (tz * 4)) : (((by * 8) + (tz * 4)) + 4),
                0:16,
                0:16,
            ],
            "wmma.accumulator",
        )
        for n_c_init in T.serial(0, 2):
            for o_c_init in T.serial(0, 4):
                T.attr(
                    [BC, Conv_wmma_accumulator],
                    "buffer_bind_scope",
                    T.tvm_tuple(
                        (n_c_init + ((bx * 8) + (ty * 2))),
                        1,
                        T.floordiv(bz, 14),
                        1,
                        T.floormod(bz, 14),
                        1,
                        (o_c_init + ((by * 8) + (tz * 4))),
                        1,
                        0,
                        16,
                        0,
                        16,
                        dtype="handle",
                    ),
                )
                T.evaluate(
                    T.tvm_fill_fragment(
                        BC.data,
                        16,
                        16,
                        16,
                        T.floordiv(BC.elem_offset, 256),
                        T.float32(0),
                        dtype="handle",
                    )
                )

        for ic_outer in T.serial(0, 8):
            for kh in T.serial(0, 3):
                T.realize(
                    Apad_shared[
                        (bx * 8) : ((bx * 8) + 8),
                        (T.floordiv(bz, 14) + kh) : ((T.floordiv(bz, 14) + kh) + 1),
                        T.floormod(bz, 14) : (T.floormod(bz, 14) + 3),
                        (ic_outer * 2) : ((ic_outer * 2) + 2),
                        0:16,
                        0:16,
                    ],
                    "shared",
                )
                for ax2 in T.serial(0, 3):
                    for ax3 in T.serial(0, 2):
                        for ax4_ax5_fused_outer in T.serial(0, 8):
                            T.launch_thread(tx, 32)
                            Apad_shared[
                                ((tz + (ty * 2)) + (bx * 8)),
                                (T.floordiv(bz, 14) + kh),
                                (ax2 + T.floormod(bz, 14)),
                                (ax3 + (ic_outer * 2)),
                                T.floordiv((tx + (ax4_ax5_fused_outer * 32)), 16),
                                T.floormod((tx + (ax4_ax5_fused_outer * 32)), 16),
                            ] = T.if_then_else(
                                (
                                    (
                                        (
                                            ((T.floordiv(bz, 14) + kh) >= 1)
                                            and (((T.floordiv(bz, 14) + kh) - 1) < 14)
                                        )
                                        and ((ax2 + T.floormod(bz, 14)) >= 1)
                                    )
                                    and (((ax2 + T.floormod(bz, 14)) - 1) < 14)
                                ),
                                A_1[
                                    ((tz + (ty * 2)) + (bx * 8)),
                                    ((T.floordiv(bz, 14) + kh) - 1),
                                    ((ax2 + T.floormod(bz, 14)) - 1),
                                    (ax3 + (ic_outer * 2)),
                                    T.floordiv((tx + (ax4_ax5_fused_outer * 32)), 16),
                                    T.floormod((tx + (ax4_ax5_fused_outer * 32)), 16),
                                ],
                                T.float16(0),
                                dtype="float16",
                            )
                T.realize(
                    W_shared[
                        kh : (kh + 1),
                        0:3,
                        (ic_outer * 2) : ((ic_outer * 2) + 2),
                        (by * 8) : ((by * 8) + 8),
                        0:16,
                        0:16,
                    ],
                    "shared",
                )
                for ax1 in T.serial(0, 3):
                    for ax2_1 in T.serial(0, 2):
                        T.launch_thread(tx, 32)
                        for ax4_ax5_fused_inner in T.vectorized(0, 8):
                            W_shared[
                                kh,
                                ax1,
                                (ax2_1 + (ic_outer * 2)),
                                ((tz + (ty * 2)) + (by * 8)),
                                T.floordiv((ax4_ax5_fused_inner + (tx * 8)), 16),
                                T.floormod((ax4_ax5_fused_inner + (tx * 8)), 16),
                            ] = W_1[
                                kh,
                                ax1,
                                (ax2_1 + (ic_outer * 2)),
                                ((tz + (ty * 2)) + (by * 8)),
                                T.floordiv((ax4_ax5_fused_inner + (tx * 8)), 16),
                                T.floormod((ax4_ax5_fused_inner + (tx * 8)), 16),
                            ]
                for ic_inner in T.serial(0, 2):
                    for kw in T.serial(0, 3):
                        T.realize(
                            Apad_shared_wmma_matrix_a[
                                ((bx * 8) + (ty * 2)) : (((bx * 8) + (ty * 2)) + 2),
                                (T.floordiv(bz, 14) + kh) : ((T.floordiv(bz, 14) + kh) + 1),
                                (kw + T.floormod(bz, 14)) : ((kw + T.floormod(bz, 14)) + 1),
                                ((ic_outer * 2) + ic_inner) : (((ic_outer * 2) + ic_inner) + 1),
                                0:16,
                                0:16,
                            ],
                            "wmma.matrix_a",
                        )
                        for ax0 in T.serial(0, 2):
                            T.attr(
                                [buffer, Apad_shared],
                                "buffer_bind_scope",
                                T.tvm_tuple(
                                    (ax0 + ((bx * 8) + (ty * 2))),
                                    1,
                                    (T.floordiv(bz, 14) + kh),
                                    1,
                                    (kw + T.floormod(bz, 14)),
                                    1,
                                    ((ic_outer * 2) + ic_inner),
                                    1,
                                    0,
                                    16,
                                    0,
                                    16,
                                    dtype="handle",
                                ),
                            )
                            T.attr(
                                [buffer_1, Apad_shared_wmma_matrix_a],
                                "buffer_bind_scope",
                                T.tvm_tuple(
                                    (ax0 + ((bx * 8) + (ty * 2))),
                                    1,
                                    (T.floordiv(bz, 14) + kh),
                                    1,
                                    (kw + T.floormod(bz, 14)),
                                    1,
                                    ((ic_outer * 2) + ic_inner),
                                    1,
                                    0,
                                    16,
                                    0,
                                    16,
                                    dtype="handle",
                                ),
                            )
                            T.evaluate(
                                T.tvm_load_matrix_sync(
                                    buffer_1.data,
                                    16,
                                    16,
                                    16,
                                    T.floordiv(buffer_1.elem_offset, 256),
                                    T.tvm_access_ptr(
                                        T.type_annotation(dtype="float16"),
                                        buffer.data,
                                        buffer.elem_offset,
                                        256,
                                        1,
                                        dtype="handle",
                                    ),
                                    16,
                                    "row_major",
                                    dtype="handle",
                                )
                            )
                        T.realize(
                            W_shared_wmma_matrix_b[
                                kh : (kh + 1),
                                kw : (kw + 1),
                                ((ic_outer * 2) + ic_inner) : (((ic_outer * 2) + ic_inner) + 1),
                                ((by * 8) + (tz * 4)) : (((by * 8) + (tz * 4)) + 4),
                                0:16,
                                0:16,
                            ],
                            "wmma.matrix_b",
                        )
                        for ax3_1 in T.serial(0, 4):
                            T.attr(
                                [buffer_2, W_shared],
                                "buffer_bind_scope",
                                T.tvm_tuple(
                                    kh,
                                    1,
                                    kw,
                                    1,
                                    ((ic_outer * 2) + ic_inner),
                                    1,
                                    (ax3_1 + ((by * 8) + (tz * 4))),
                                    1,
                                    0,
                                    16,
                                    0,
                                    16,
                                    dtype="handle",
                                ),
                            )
                            T.attr(
                                [buffer_3, W_shared_wmma_matrix_b],
                                "buffer_bind_scope",
                                T.tvm_tuple(
                                    kh,
                                    1,
                                    kw,
                                    1,
                                    ((ic_outer * 2) + ic_inner),
                                    1,
                                    (ax3_1 + ((by * 8) + (tz * 4))),
                                    1,
                                    0,
                                    16,
                                    0,
                                    16,
                                    dtype="handle",
                                ),
                            )
                            T.evaluate(
                                T.tvm_load_matrix_sync(
                                    buffer_3.data,
                                    16,
                                    16,
                                    16,
                                    T.floordiv(buffer_3.elem_offset, 256),
                                    T.tvm_access_ptr(
                                        T.type_annotation(dtype="float16"),
                                        buffer_2.data,
                                        buffer_2.elem_offset,
                                        256,
                                        1,
                                        dtype="handle",
                                    ),
                                    16,
                                    "row_major",
                                    dtype="handle",
                                )
                            )
                        for n_c in T.serial(0, 2):
                            for o_c in T.serial(0, 4):
                                T.attr(
                                    [BA, Apad_shared_wmma_matrix_a],
                                    "buffer_bind_scope",
                                    T.tvm_tuple(
                                        (n_c + ((bx * 8) + (ty * 2))),
                                        1,
                                        (T.floordiv(bz, 14) + kh),
                                        1,
                                        (T.floormod(bz, 14) + kw),
                                        1,
                                        ((ic_outer * 2) + ic_inner),
                                        1,
                                        0,
                                        16,
                                        0,
                                        16,
                                        dtype="handle",
                                    ),
                                )
                                T.attr(
                                    [BB, W_shared_wmma_matrix_b],
                                    "buffer_bind_scope",
                                    T.tvm_tuple(
                                        kh,
                                        1,
                                        kw,
                                        1,
                                        ((ic_outer * 2) + ic_inner),
                                        1,
                                        (o_c + ((by * 8) + (tz * 4))),
                                        1,
                                        0,
                                        16,
                                        0,
                                        16,
                                        dtype="handle",
                                    ),
                                )
                                T.attr(
                                    [BC, Conv_wmma_accumulator],
                                    "buffer_bind_scope",
                                    T.tvm_tuple(
                                        (n_c + ((bx * 8) + (ty * 2))),
                                        1,
                                        T.floordiv(bz, 14),
                                        1,
                                        T.floormod(bz, 14),
                                        1,
                                        (o_c + ((by * 8) + (tz * 4))),
                                        1,
                                        0,
                                        16,
                                        0,
                                        16,
                                        dtype="handle",
                                    ),
                                )
                                T.evaluate(
                                    T.tvm_mma_sync(
                                        BC.data,
                                        T.floordiv(BC.elem_offset, 256),
                                        BA.data,
                                        T.floordiv(BA.elem_offset, 256),
                                        BB.data,
                                        T.floordiv(BB.elem_offset, 256),
                                        BC.data,
                                        T.floordiv(BC.elem_offset, 256),
                                        dtype="handle",
                                    )
                                )
        for n_inner in T.serial(0, 2):
            for o_inner in T.serial(0, 4):
                T.attr(
                    [buffer_4, Conv_wmma_accumulator],
                    "buffer_bind_scope",
                    T.tvm_tuple(
                        ((((bx * 4) + ty) * 2) + n_inner),
                        1,
                        T.floordiv(bz, 14),
                        1,
                        T.floormod(bz, 14),
                        1,
                        ((((by * 2) + tz) * 4) + o_inner),
                        1,
                        0,
                        16,
                        0,
                        16,
                        dtype="handle",
                    ),
                )
                T.attr(
                    [buffer_5, Conv_1],
                    "buffer_bind_scope",
                    T.tvm_tuple(
                        ((((bx * 4) + ty) * 2) + n_inner),
                        1,
                        T.floordiv(bz, 14),
                        1,
                        T.floormod(bz, 14),
                        1,
                        ((((by * 2) + tz) * 4) + o_inner),
                        1,
                        0,
                        16,
                        0,
                        16,
                        dtype="handle",
                    ),
                )
                T.evaluate(
                    T.tvm_store_matrix_sync(
                        buffer_4.data,
                        16,
                        16,
                        16,
                        T.floordiv(buffer_4.elem_offset, 256),
                        T.tvm_access_ptr(
                            T.type_annotation(dtype="float32"),
                            buffer_5.data,
                            buffer_5.elem_offset,
                            256,
                            2,
                            dtype="handle",
                        ),
                        16,
                        "row_major",
                        dtype="handle",
                    )
                )

    return func


def opt_conv_tensorcore_lower():
    @T.prim_func
    def func(
        A: T.Buffer[(16, 14, 14, 16, 16, 16), "float16"],
        W: T.Buffer[(3, 3, 16, 32, 16, 16), "float16"],
        Conv: T.Buffer[(16, 14, 14, 32, 16, 16), "float32"],
    ) -> None:
        # function attr dict
        T.func_attr({"global_symbol": "default_function", "tir.noalias": True})
        # body
        A_1 = T.buffer_decl([12845056], dtype="float16", data=A.data)
        W_1 = T.buffer_decl([1179648], dtype="float16", data=W.data)
        Conv_1 = T.buffer_decl([25690112], data=Conv.data)
        bx = T.env_thread("blockIdx.x")
        by = T.env_thread("blockIdx.y")
        bz = T.env_thread("blockIdx.z")
        tx = T.env_thread("threadIdx.x")
        ty = T.env_thread("threadIdx.y")
        tz = T.env_thread("threadIdx.z")
        T.launch_thread(bz, 196)
        Conv_wmma_accumulator = T.allocate([2048], "float32", "wmma.accumulator")
        Apad_shared = T.allocate([12288], "float16", "shared")
        W_shared = T.allocate([12288], "float16", "shared")
        Apad_shared_wmma_matrix_a = T.allocate([512], "float16", "wmma.matrix_a")
        W_shared_wmma_matrix_b = T.allocate([1024], "float16", "wmma.matrix_b")
        T.launch_thread(bx, 2)
        T.launch_thread(by, 4)
        T.launch_thread(ty, 4)
        T.launch_thread(tz, 2)
        T.evaluate(
            T.tvm_fill_fragment(
                Conv_wmma_accumulator.data, 16, 16, 16, 0, T.float32(0), dtype="handle"
            )
        )
        T.evaluate(
            T.tvm_fill_fragment(
                Conv_wmma_accumulator.data, 16, 16, 16, 1, T.float32(0), dtype="handle"
            )
        )
        T.evaluate(
            T.tvm_fill_fragment(
                Conv_wmma_accumulator.data, 16, 16, 16, 2, T.float32(0), dtype="handle"
            )
        )
        T.evaluate(
            T.tvm_fill_fragment(
                Conv_wmma_accumulator.data, 16, 16, 16, 3, T.float32(0), dtype="handle"
            )
        )
        T.evaluate(
            T.tvm_fill_fragment(
                Conv_wmma_accumulator.data, 16, 16, 16, 4, T.float32(0), dtype="handle"
            )
        )
        T.evaluate(
            T.tvm_fill_fragment(
                Conv_wmma_accumulator.data, 16, 16, 16, 5, T.float32(0), dtype="handle"
            )
        )
        T.evaluate(
            T.tvm_fill_fragment(
                Conv_wmma_accumulator.data, 16, 16, 16, 6, T.float32(0), dtype="handle"
            )
        )
        T.evaluate(
            T.tvm_fill_fragment(
                Conv_wmma_accumulator.data, 16, 16, 16, 7, T.float32(0), dtype="handle"
            )
        )
        for ic_outer in T.serial(0, 8):
            for kh in T.serial(0, 3):
                for ax2 in T.serial(0, 3):
                    with T.launch_thread(tx, 32):
                        Apad_shared[
                            ((((ty * 3072) + (tz * 1536)) + (ax2 * 512)) + tx)
                        ] = T.if_then_else(
                            (
                                (
                                    (
                                        (1 <= (T.floordiv(bz, 14) + kh))
                                        and ((T.floordiv(bz, 14) + kh) < 15)
                                    )
                                    and (1 <= (ax2 + T.floormod(bz, 14)))
                                )
                                and ((ax2 + T.floormod(bz, 14)) < 15)
                            ),
                            A_1[
                                (
                                    (
                                        (
                                            (
                                                (
                                                    (
                                                        (
                                                            ((bx * 6422528) + (ty * 1605632))
                                                            + (tz * 802816)
                                                        )
                                                        + (kh * 57344)
                                                    )
                                                    + (bz * 4096)
                                                )
                                                + (ax2 * 4096)
                                            )
                                            + (ic_outer * 512)
                                        )
                                        + tx
                                    )
                                    - 61440
                                ),
                            ],
                            T.float16(0),
                            dtype="float16",
                        )
                    with T.launch_thread(tx, 32):
                        Apad_shared[
                            (((((ty * 3072) + (tz * 1536)) + (ax2 * 512)) + tx) + 32)
                        ] = T.if_then_else(
                            (
                                (
                                    (
                                        (1 <= (T.floordiv(bz, 14) + kh))
                                        and ((T.floordiv(bz, 14) + kh) < 15)
                                    )
                                    and (1 <= (ax2 + T.floormod(bz, 14)))
                                )
                                and ((ax2 + T.floormod(bz, 14)) < 15)
                            ),
                            A_1[
                                (
                                    (
                                        (
                                            (
                                                (
                                                    (
                                                        (
                                                            ((bx * 6422528) + (ty * 1605632))
                                                            + (tz * 802816)
                                                        )
                                                        + (kh * 57344)
                                                    )
                                                    + (bz * 4096)
                                                )
                                                + (ax2 * 4096)
                                            )
                                            + (ic_outer * 512)
                                        )
                                        + tx
                                    )
                                    - 61408
                                ),
                            ],
                            T.float16(0),
                            dtype="float16",
                        )
                    with T.launch_thread(tx, 32):
                        Apad_shared[
                            (((((ty * 3072) + (tz * 1536)) + (ax2 * 512)) + tx) + 64)
                        ] = T.if_then_else(
                            (
                                (
                                    (
                                        (1 <= (T.floordiv(bz, 14) + kh))
                                        and ((T.floordiv(bz, 14) + kh) < 15)
                                    )
                                    and (1 <= (ax2 + T.floormod(bz, 14)))
                                )
                                and ((ax2 + T.floormod(bz, 14)) < 15)
                            ),
                            A_1[
                                (
                                    (
                                        (
                                            (
                                                (
                                                    (
                                                        (
                                                            ((bx * 6422528) + (ty * 1605632))
                                                            + (tz * 802816)
                                                        )
                                                        + (kh * 57344)
                                                    )
                                                    + (bz * 4096)
                                                )
                                                + (ax2 * 4096)
                                            )
                                            + (ic_outer * 512)
                                        )
                                        + tx
                                    )
                                    - 61376
                                ),
                            ],
                            T.float16(0),
                            dtype="float16",
                        )
                    with T.launch_thread(tx, 32):
                        Apad_shared[
                            (((((ty * 3072) + (tz * 1536)) + (ax2 * 512)) + tx) + 96)
                        ] = T.if_then_else(
                            (
                                (
                                    (
                                        (1 <= (T.floordiv(bz, 14) + kh))
                                        and ((T.floordiv(bz, 14) + kh) < 15)
                                    )
                                    and (1 <= (ax2 + T.floormod(bz, 14)))
                                )
                                and ((ax2 + T.floormod(bz, 14)) < 15)
                            ),
                            A_1[
                                (
                                    (
                                        (
                                            (
                                                (
                                                    (
                                                        (
                                                            ((bx * 6422528) + (ty * 1605632))
                                                            + (tz * 802816)
                                                        )
                                                        + (kh * 57344)
                                                    )
                                                    + (bz * 4096)
                                                )
                                                + (ax2 * 4096)
                                            )
                                            + (ic_outer * 512)
                                        )
                                        + tx
                                    )
                                    - 61344
                                ),
                            ],
                            T.float16(0),
                            dtype="float16",
                        )
                    with T.launch_thread(tx, 32):
                        Apad_shared[
                            (((((ty * 3072) + (tz * 1536)) + (ax2 * 512)) + tx) + 128)
                        ] = T.if_then_else(
                            (
                                (
                                    (
                                        (1 <= (T.floordiv(bz, 14) + kh))
                                        and ((T.floordiv(bz, 14) + kh) < 15)
                                    )
                                    and (1 <= (ax2 + T.floormod(bz, 14)))
                                )
                                and ((ax2 + T.floormod(bz, 14)) < 15)
                            ),
                            A_1[
                                (
                                    (
                                        (
                                            (
                                                (
                                                    (
                                                        (
                                                            ((bx * 6422528) + (ty * 1605632))
                                                            + (tz * 802816)
                                                        )
                                                        + (kh * 57344)
                                                    )
                                                    + (bz * 4096)
                                                )
                                                + (ax2 * 4096)
                                            )
                                            + (ic_outer * 512)
                                        )
                                        + tx
                                    )
                                    - 61312
                                ),
                            ],
                            T.float16(0),
                            dtype="float16",
                        )
                    with T.launch_thread(tx, 32):
                        Apad_shared[
                            (((((ty * 3072) + (tz * 1536)) + (ax2 * 512)) + tx) + 160)
                        ] = T.if_then_else(
                            (
                                (
                                    (
                                        (1 <= (T.floordiv(bz, 14) + kh))
                                        and ((T.floordiv(bz, 14) + kh) < 15)
                                    )
                                    and (1 <= (ax2 + T.floormod(bz, 14)))
                                )
                                and ((ax2 + T.floormod(bz, 14)) < 15)
                            ),
                            A_1[
                                (
                                    (
                                        (
                                            (
                                                (
                                                    (
                                                        (
                                                            ((bx * 6422528) + (ty * 1605632))
                                                            + (tz * 802816)
                                                        )
                                                        + (kh * 57344)
                                                    )
                                                    + (bz * 4096)
                                                )
                                                + (ax2 * 4096)
                                            )
                                            + (ic_outer * 512)
                                        )
                                        + tx
                                    )
                                    - 61280
                                ),
                            ],
                            T.float16(0),
                            dtype="float16",
                        )
                    with T.launch_thread(tx, 32):
                        Apad_shared[
                            (((((ty * 3072) + (tz * 1536)) + (ax2 * 512)) + tx) + 192)
                        ] = T.if_then_else(
                            (
                                (
                                    (
                                        (1 <= (T.floordiv(bz, 14) + kh))
                                        and ((T.floordiv(bz, 14) + kh) < 15)
                                    )
                                    and (1 <= (ax2 + T.floormod(bz, 14)))
                                )
                                and ((ax2 + T.floormod(bz, 14)) < 15)
                            ),
                            A_1[
                                (
                                    (
                                        (
                                            (
                                                (
                                                    (
                                                        (
                                                            ((bx * 6422528) + (ty * 1605632))
                                                            + (tz * 802816)
                                                        )
                                                        + (kh * 57344)
                                                    )
                                                    + (bz * 4096)
                                                )
                                                + (ax2 * 4096)
                                            )
                                            + (ic_outer * 512)
                                        )
                                        + tx
                                    )
                                    - 61248
                                ),
                            ],
                            T.float16(0),
                            dtype="float16",
                        )
                    with T.launch_thread(tx, 32):
                        Apad_shared[
                            (((((ty * 3072) + (tz * 1536)) + (ax2 * 512)) + tx) + 224)
                        ] = T.if_then_else(
                            (
                                (
                                    (
                                        (1 <= (T.floordiv(bz, 14) + kh))
                                        and ((T.floordiv(bz, 14) + kh) < 15)
                                    )
                                    and (1 <= (ax2 + T.floormod(bz, 14)))
                                )
                                and ((ax2 + T.floormod(bz, 14)) < 15)
                            ),
                            A_1[
                                (
                                    (
                                        (
                                            (
                                                (
                                                    (
                                                        (
                                                            ((bx * 6422528) + (ty * 1605632))
                                                            + (tz * 802816)
                                                        )
                                                        + (kh * 57344)
                                                    )
                                                    + (bz * 4096)
                                                )
                                                + (ax2 * 4096)
                                            )
                                            + (ic_outer * 512)
                                        )
                                        + tx
                                    )
                                    - 61216
                                ),
                            ],
                            T.float16(0),
                            dtype="float16",
                        )
                    with T.launch_thread(tx, 32):
                        Apad_shared[
                            (((((ty * 3072) + (tz * 1536)) + (ax2 * 512)) + tx) + 256)
                        ] = T.if_then_else(
                            (
                                (
                                    (
                                        (1 <= (T.floordiv(bz, 14) + kh))
                                        and ((T.floordiv(bz, 14) + kh) < 15)
                                    )
                                    and (1 <= (ax2 + T.floormod(bz, 14)))
                                )
                                and ((ax2 + T.floormod(bz, 14)) < 15)
                            ),
                            A_1[
                                (
                                    (
                                        (
                                            (
                                                (
                                                    (
                                                        (
                                                            ((bx * 6422528) + (ty * 1605632))
                                                            + (tz * 802816)
                                                        )
                                                        + (kh * 57344)
                                                    )
                                                    + (bz * 4096)
                                                )
                                                + (ax2 * 4096)
                                            )
                                            + (ic_outer * 512)
                                        )
                                        + tx
                                    )
                                    - 61184
                                ),
                            ],
                            T.float16(0),
                            dtype="float16",
                        )
                    with T.launch_thread(tx, 32):
                        Apad_shared[
                            (((((ty * 3072) + (tz * 1536)) + (ax2 * 512)) + tx) + 288)
                        ] = T.if_then_else(
                            (
                                (
                                    (
                                        (1 <= (T.floordiv(bz, 14) + kh))
                                        and ((T.floordiv(bz, 14) + kh) < 15)
                                    )
                                    and (1 <= (ax2 + T.floormod(bz, 14)))
                                )
                                and ((ax2 + T.floormod(bz, 14)) < 15)
                            ),
                            A_1[
                                (
                                    (
                                        (
                                            (
                                                (
                                                    (
                                                        (
                                                            ((bx * 6422528) + (ty * 1605632))
                                                            + (tz * 802816)
                                                        )
                                                        + (kh * 57344)
                                                    )
                                                    + (bz * 4096)
                                                )
                                                + (ax2 * 4096)
                                            )
                                            + (ic_outer * 512)
                                        )
                                        + tx
                                    )
                                    - 61152
                                ),
                            ],
                            T.float16(0),
                            dtype="float16",
                        )
                    with T.launch_thread(tx, 32):
                        Apad_shared[
                            (((((ty * 3072) + (tz * 1536)) + (ax2 * 512)) + tx) + 320)
                        ] = T.if_then_else(
                            (
                                (
                                    (
                                        (1 <= (T.floordiv(bz, 14) + kh))
                                        and ((T.floordiv(bz, 14) + kh) < 15)
                                    )
                                    and (1 <= (ax2 + T.floormod(bz, 14)))
                                )
                                and ((ax2 + T.floormod(bz, 14)) < 15)
                            ),
                            A_1[
                                (
                                    (
                                        (
                                            (
                                                (
                                                    (
                                                        (
                                                            ((bx * 6422528) + (ty * 1605632))
                                                            + (tz * 802816)
                                                        )
                                                        + (kh * 57344)
                                                    )
                                                    + (bz * 4096)
                                                )
                                                + (ax2 * 4096)
                                            )
                                            + (ic_outer * 512)
                                        )
                                        + tx
                                    )
                                    - 61120
                                ),
                            ],
                            T.float16(0),
                            dtype="float16",
                        )
                    with T.launch_thread(tx, 32):
                        Apad_shared[
                            (((((ty * 3072) + (tz * 1536)) + (ax2 * 512)) + tx) + 352)
                        ] = T.if_then_else(
                            (
                                (
                                    (
                                        (1 <= (T.floordiv(bz, 14) + kh))
                                        and ((T.floordiv(bz, 14) + kh) < 15)
                                    )
                                    and (1 <= (ax2 + T.floormod(bz, 14)))
                                )
                                and ((ax2 + T.floormod(bz, 14)) < 15)
                            ),
                            A_1[
                                (
                                    (
                                        (
                                            (
                                                (
                                                    (
                                                        (
                                                            ((bx * 6422528) + (ty * 1605632))
                                                            + (tz * 802816)
                                                        )
                                                        + (kh * 57344)
                                                    )
                                                    + (bz * 4096)
                                                )
                                                + (ax2 * 4096)
                                            )
                                            + (ic_outer * 512)
                                        )
                                        + tx
                                    )
                                    - 61088
                                ),
                            ],
                            T.float16(0),
                            dtype="float16",
                        )
                    with T.launch_thread(tx, 32):
                        Apad_shared[
                            (((((ty * 3072) + (tz * 1536)) + (ax2 * 512)) + tx) + 384)
                        ] = T.if_then_else(
                            (
                                (
                                    (
                                        (1 <= (T.floordiv(bz, 14) + kh))
                                        and ((T.floordiv(bz, 14) + kh) < 15)
                                    )
                                    and (1 <= (ax2 + T.floormod(bz, 14)))
                                )
                                and ((ax2 + T.floormod(bz, 14)) < 15)
                            ),
                            A_1[
                                (
                                    (
                                        (
                                            (
                                                (
                                                    (
                                                        (
                                                            ((bx * 6422528) + (ty * 1605632))
                                                            + (tz * 802816)
                                                        )
                                                        + (kh * 57344)
                                                    )
                                                    + (bz * 4096)
                                                )
                                                + (ax2 * 4096)
                                            )
                                            + (ic_outer * 512)
                                        )
                                        + tx
                                    )
                                    - 61056
                                ),
                            ],
                            T.float16(0),
                            dtype="float16",
                        )
                    with T.launch_thread(tx, 32):
                        Apad_shared[
                            (((((ty * 3072) + (tz * 1536)) + (ax2 * 512)) + tx) + 416)
                        ] = T.if_then_else(
                            (
                                (
                                    (
                                        (1 <= (T.floordiv(bz, 14) + kh))
                                        and ((T.floordiv(bz, 14) + kh) < 15)
                                    )
                                    and (1 <= (ax2 + T.floormod(bz, 14)))
                                )
                                and ((ax2 + T.floormod(bz, 14)) < 15)
                            ),
                            A_1[
                                (
                                    (
                                        (
                                            (
                                                (
                                                    (
                                                        (
                                                            ((bx * 6422528) + (ty * 1605632))
                                                            + (tz * 802816)
                                                        )
                                                        + (kh * 57344)
                                                    )
                                                    + (bz * 4096)
                                                )
                                                + (ax2 * 4096)
                                            )
                                            + (ic_outer * 512)
                                        )
                                        + tx
                                    )
                                    - 61024
                                ),
                            ],
                            T.float16(0),
                            dtype="float16",
                        )
                    with T.launch_thread(tx, 32):
                        Apad_shared[
                            (((((ty * 3072) + (tz * 1536)) + (ax2 * 512)) + tx) + 448)
                        ] = T.if_then_else(
                            (
                                (
                                    (
                                        (1 <= (T.floordiv(bz, 14) + kh))
                                        and ((T.floordiv(bz, 14) + kh) < 15)
                                    )
                                    and (1 <= (ax2 + T.floormod(bz, 14)))
                                )
                                and ((ax2 + T.floormod(bz, 14)) < 15)
                            ),
                            A_1[
                                (
                                    (
                                        (
                                            (
                                                (
                                                    (
                                                        (
                                                            ((bx * 6422528) + (ty * 1605632))
                                                            + (tz * 802816)
                                                        )
                                                        + (kh * 57344)
                                                    )
                                                    + (bz * 4096)
                                                )
                                                + (ax2 * 4096)
                                            )
                                            + (ic_outer * 512)
                                        )
                                        + tx
                                    )
                                    - 60992
                                ),
                            ],
                            T.float16(0),
                            dtype="float16",
                        )
                    T.launch_thread(tx, 32)
                    Apad_shared[
                        (((((ty * 3072) + (tz * 1536)) + (ax2 * 512)) + tx) + 480)
                    ] = T.if_then_else(
                        (
                            (
                                (
                                    (1 <= (T.floordiv(bz, 14) + kh))
                                    and ((T.floordiv(bz, 14) + kh) < 15)
                                )
                                and (1 <= (ax2 + T.floormod(bz, 14)))
                            )
                            and ((ax2 + T.floormod(bz, 14)) < 15)
                        ),
                        A_1[
                            (
                                (
                                    (
                                        (
                                            (
                                                (
                                                    (
                                                        ((bx * 6422528) + (ty * 1605632))
                                                        + (tz * 802816)
                                                    )
                                                    + (kh * 57344)
                                                )
                                                + (bz * 4096)
                                            )
                                            + (ax2 * 4096)
                                        )
                                        + (ic_outer * 512)
                                    )
                                    + tx
                                )
                                - 60960
                            ),
                        ],
                        T.float16(0),
                        dtype="float16",
                    )
                with T.launch_thread(tx, 32):
                    W_shared[T.ramp((((ty * 512) + (tz * 256)) + (tx * 8)), 1, 8)] = W_1[
                        T.ramp(
                            (
                                (
                                    (
                                        (((kh * 393216) + (ic_outer * 16384)) + (by * 2048))
                                        + (ty * 512)
                                    )
                                    + (tz * 256)
                                )
                                + (tx * 8)
                            ),
                            1,
                            8,
                        )
                    ]
                with T.launch_thread(tx, 32):
                    W_shared[T.ramp(((((ty * 512) + (tz * 256)) + (tx * 8)) + 2048), 1, 8)] = W_1[
                        T.ramp(
                            (
                                (
                                    (
                                        (
                                            (((kh * 393216) + (ic_outer * 16384)) + (by * 2048))
                                            + (ty * 512)
                                        )
                                        + (tz * 256)
                                    )
                                    + (tx * 8)
                                )
                                + 8192
                            ),
                            1,
                            8,
                        )
                    ]
                with T.launch_thread(tx, 32):
                    W_shared[T.ramp(((((ty * 512) + (tz * 256)) + (tx * 8)) + 4096), 1, 8)] = W_1[
                        T.ramp(
                            (
                                (
                                    (
                                        (
                                            (((kh * 393216) + (ic_outer * 16384)) + (by * 2048))
                                            + (ty * 512)
                                        )
                                        + (tz * 256)
                                    )
                                    + (tx * 8)
                                )
                                + 131072
                            ),
                            1,
                            8,
                        )
                    ]
                with T.launch_thread(tx, 32):
                    W_shared[T.ramp(((((ty * 512) + (tz * 256)) + (tx * 8)) + 6144), 1, 8)] = W_1[
                        T.ramp(
                            (
                                (
                                    (
                                        (
                                            (((kh * 393216) + (ic_outer * 16384)) + (by * 2048))
                                            + (ty * 512)
                                        )
                                        + (tz * 256)
                                    )
                                    + (tx * 8)
                                )
                                + 139264
                            ),
                            1,
                            8,
                        )
                    ]
                with T.launch_thread(tx, 32):
                    W_shared[T.ramp(((((ty * 512) + (tz * 256)) + (tx * 8)) + 8192), 1, 8)] = W_1[
                        T.ramp(
                            (
                                (
                                    (
                                        (
                                            (((kh * 393216) + (ic_outer * 16384)) + (by * 2048))
                                            + (ty * 512)
                                        )
                                        + (tz * 256)
                                    )
                                    + (tx * 8)
                                )
                                + 262144
                            ),
                            1,
                            8,
                        )
                    ]
                with T.launch_thread(tx, 32):
                    W_shared[T.ramp(((((ty * 512) + (tz * 256)) + (tx * 8)) + 10240), 1, 8)] = W_1[
                        T.ramp(
                            (
                                (
                                    (
                                        (
                                            (((kh * 393216) + (ic_outer * 16384)) + (by * 2048))
                                            + (ty * 512)
                                        )
                                        + (tz * 256)
                                    )
                                    + (tx * 8)
                                )
                                + 270336
                            ),
                            1,
                            8,
                        )
                    ]
                for ic_inner in T.serial(0, 2):
                    for kw in T.serial(0, 3):
                        T.evaluate(
                            T.tvm_load_matrix_sync(
                                Apad_shared_wmma_matrix_a.data,
                                16,
                                16,
                                16,
                                0,
                                T.tvm_access_ptr(
                                    T.type_annotation(dtype="float16"),
                                    Apad_shared.data,
                                    (((ty * 3072) + (kw * 512)) + (ic_inner * 256)),
                                    256,
                                    1,
                                    dtype="handle",
                                ),
                                16,
                                "row_major",
                                dtype="handle",
                            )
                        )
                        T.evaluate(
                            T.tvm_load_matrix_sync(
                                Apad_shared_wmma_matrix_a.data,
                                16,
                                16,
                                16,
                                1,
                                T.tvm_access_ptr(
                                    T.type_annotation(dtype="float16"),
                                    Apad_shared.data,
                                    ((((ty * 3072) + (kw * 512)) + (ic_inner * 256)) + 1536),
                                    256,
                                    1,
                                    dtype="handle",
                                ),
                                16,
                                "row_major",
                                dtype="handle",
                            )
                        )
                        T.evaluate(
                            T.tvm_load_matrix_sync(
                                W_shared_wmma_matrix_b.data,
                                16,
                                16,
                                16,
                                0,
                                T.tvm_access_ptr(
                                    T.type_annotation(dtype="float16"),
                                    W_shared.data,
                                    (((kw * 4096) + (ic_inner * 2048)) + (tz * 1024)),
                                    256,
                                    1,
                                    dtype="handle",
                                ),
                                16,
                                "row_major",
                                dtype="handle",
                            )
                        )
                        T.evaluate(
                            T.tvm_load_matrix_sync(
                                W_shared_wmma_matrix_b.data,
                                16,
                                16,
                                16,
                                1,
                                T.tvm_access_ptr(
                                    T.type_annotation(dtype="float16"),
                                    W_shared.data,
                                    ((((kw * 4096) + (ic_inner * 2048)) + (tz * 1024)) + 256),
                                    256,
                                    1,
                                    dtype="handle",
                                ),
                                16,
                                "row_major",
                                dtype="handle",
                            )
                        )
                        T.evaluate(
                            T.tvm_load_matrix_sync(
                                W_shared_wmma_matrix_b.data,
                                16,
                                16,
                                16,
                                2,
                                T.tvm_access_ptr(
                                    T.type_annotation(dtype="float16"),
                                    W_shared.data,
                                    ((((kw * 4096) + (ic_inner * 2048)) + (tz * 1024)) + 512),
                                    256,
                                    1,
                                    dtype="handle",
                                ),
                                16,
                                "row_major",
                                dtype="handle",
                            )
                        )
                        T.evaluate(
                            T.tvm_load_matrix_sync(
                                W_shared_wmma_matrix_b.data,
                                16,
                                16,
                                16,
                                3,
                                T.tvm_access_ptr(
                                    T.type_annotation(dtype="float16"),
                                    W_shared.data,
                                    ((((kw * 4096) + (ic_inner * 2048)) + (tz * 1024)) + 768),
                                    256,
                                    1,
                                    dtype="handle",
                                ),
                                16,
                                "row_major",
                                dtype="handle",
                            )
                        )
                        T.evaluate(
                            T.tvm_mma_sync(
                                Conv_wmma_accumulator.data,
                                0,
                                Apad_shared_wmma_matrix_a.data,
                                0,
                                W_shared_wmma_matrix_b.data,
                                0,
                                Conv_wmma_accumulator.data,
                                0,
                                dtype="handle",
                            )
                        )
                        T.evaluate(
                            T.tvm_mma_sync(
                                Conv_wmma_accumulator.data,
                                1,
                                Apad_shared_wmma_matrix_a.data,
                                0,
                                W_shared_wmma_matrix_b.data,
                                1,
                                Conv_wmma_accumulator.data,
                                1,
                                dtype="handle",
                            )
                        )
                        T.evaluate(
                            T.tvm_mma_sync(
                                Conv_wmma_accumulator.data,
                                2,
                                Apad_shared_wmma_matrix_a.data,
                                0,
                                W_shared_wmma_matrix_b.data,
                                2,
                                Conv_wmma_accumulator.data,
                                2,
                                dtype="handle",
                            )
                        )
                        T.evaluate(
                            T.tvm_mma_sync(
                                Conv_wmma_accumulator.data,
                                3,
                                Apad_shared_wmma_matrix_a.data,
                                0,
                                W_shared_wmma_matrix_b.data,
                                3,
                                Conv_wmma_accumulator.data,
                                3,
                                dtype="handle",
                            )
                        )
                        T.evaluate(
                            T.tvm_mma_sync(
                                Conv_wmma_accumulator.data,
                                4,
                                Apad_shared_wmma_matrix_a.data,
                                1,
                                W_shared_wmma_matrix_b.data,
                                0,
                                Conv_wmma_accumulator.data,
                                4,
                                dtype="handle",
                            )
                        )
                        T.evaluate(
                            T.tvm_mma_sync(
                                Conv_wmma_accumulator.data,
                                5,
                                Apad_shared_wmma_matrix_a.data,
                                1,
                                W_shared_wmma_matrix_b.data,
                                1,
                                Conv_wmma_accumulator.data,
                                5,
                                dtype="handle",
                            )
                        )
                        T.evaluate(
                            T.tvm_mma_sync(
                                Conv_wmma_accumulator.data,
                                6,
                                Apad_shared_wmma_matrix_a.data,
                                1,
                                W_shared_wmma_matrix_b.data,
                                2,
                                Conv_wmma_accumulator.data,
                                6,
                                dtype="handle",
                            )
                        )
                        T.evaluate(
                            T.tvm_mma_sync(
                                Conv_wmma_accumulator.data,
                                7,
                                Apad_shared_wmma_matrix_a.data,
                                1,
                                W_shared_wmma_matrix_b.data,
                                3,
                                Conv_wmma_accumulator.data,
                                7,
                                dtype="handle",
                            )
                        )
        T.evaluate(
            T.tvm_store_matrix_sync(
                Conv_wmma_accumulator.data,
                16,
                16,
                16,
                0,
                T.tvm_access_ptr(
                    T.type_annotation(dtype="float32"),
                    Conv_1.data,
                    (
                        ((((bx * 12845056) + (ty * 3211264)) + (bz * 8192)) + (by * 2048))
                        + (tz * 1024)
                    ),
                    256,
                    2,
                    dtype="handle",
                ),
                16,
                "row_major",
                dtype="handle",
            )
        )
        T.evaluate(
            T.tvm_store_matrix_sync(
                Conv_wmma_accumulator.data,
                16,
                16,
                16,
                1,
                T.tvm_access_ptr(
                    T.type_annotation(dtype="float32"),
                    Conv_1.data,
                    (
                        (
                            ((((bx * 12845056) + (ty * 3211264)) + (bz * 8192)) + (by * 2048))
                            + (tz * 1024)
                        )
                        + 256
                    ),
                    256,
                    2,
                    dtype="handle",
                ),
                16,
                "row_major",
                dtype="handle",
            )
        )
        T.evaluate(
            T.tvm_store_matrix_sync(
                Conv_wmma_accumulator.data,
                16,
                16,
                16,
                2,
                T.tvm_access_ptr(
                    T.type_annotation(dtype="float32"),
                    Conv_1.data,
                    (
                        (
                            ((((bx * 12845056) + (ty * 3211264)) + (bz * 8192)) + (by * 2048))
                            + (tz * 1024)
                        )
                        + 512
                    ),
                    256,
                    2,
                    dtype="handle",
                ),
                16,
                "row_major",
                dtype="handle",
            )
        )
        T.evaluate(
            T.tvm_store_matrix_sync(
                Conv_wmma_accumulator.data,
                16,
                16,
                16,
                3,
                T.tvm_access_ptr(
                    T.type_annotation(dtype="float32"),
                    Conv_1.data,
                    (
                        (
                            ((((bx * 12845056) + (ty * 3211264)) + (bz * 8192)) + (by * 2048))
                            + (tz * 1024)
                        )
                        + 768
                    ),
                    256,
                    2,
                    dtype="handle",
                ),
                16,
                "row_major",
                dtype="handle",
            )
        )
        T.evaluate(
            T.tvm_store_matrix_sync(
                Conv_wmma_accumulator.data,
                16,
                16,
                16,
                4,
                T.tvm_access_ptr(
                    T.type_annotation(dtype="float32"),
                    Conv_1.data,
                    (
                        (
                            ((((bx * 12845056) + (ty * 3211264)) + (bz * 8192)) + (by * 2048))
                            + (tz * 1024)
                        )
                        + 1605632
                    ),
                    256,
                    2,
                    dtype="handle",
                ),
                16,
                "row_major",
                dtype="handle",
            )
        )
        T.evaluate(
            T.tvm_store_matrix_sync(
                Conv_wmma_accumulator.data,
                16,
                16,
                16,
                5,
                T.tvm_access_ptr(
                    T.type_annotation(dtype="float32"),
                    Conv_1.data,
                    (
                        (
                            ((((bx * 12845056) + (ty * 3211264)) + (bz * 8192)) + (by * 2048))
                            + (tz * 1024)
                        )
                        + 1605888
                    ),
                    256,
                    2,
                    dtype="handle",
                ),
                16,
                "row_major",
                dtype="handle",
            )
        )
        T.evaluate(
            T.tvm_store_matrix_sync(
                Conv_wmma_accumulator.data,
                16,
                16,
                16,
                6,
                T.tvm_access_ptr(
                    T.type_annotation(dtype="float32"),
                    Conv_1.data,
                    (
                        (
                            ((((bx * 12845056) + (ty * 3211264)) + (bz * 8192)) + (by * 2048))
                            + (tz * 1024)
                        )
                        + 1606144
                    ),
                    256,
                    2,
                    dtype="handle",
                ),
                16,
                "row_major",
                dtype="handle",
            )
        )
        T.evaluate(
            T.tvm_store_matrix_sync(
                Conv_wmma_accumulator.data,
                16,
                16,
                16,
                7,
                T.tvm_access_ptr(
                    T.type_annotation(dtype="float32"),
                    Conv_1.data,
                    (
                        (
                            ((((bx * 12845056) + (ty * 3211264)) + (bz * 8192)) + (by * 2048))
                            + (tz * 1024)
                        )
                        + 1606400
                    ),
                    256,
                    2,
                    dtype="handle",
                ),
                16,
                "row_major",
                dtype="handle",
            )
        )

    return func


def opt_conv_tensorcore_mod_host():
    @T.prim_func
    def opt_conv_tensorcore_mod_host(
        args: T.handle,
        arg_type_ids: T.Buffer[(3,), "int32"],
        num_args: T.int32,
        out_ret_value: T.handle,
        out_ret_tcode: T.handle,
        resource_handle: T.handle,
    ) -> T.int32:
        # function attr dict
        T.func_attr(
            {
                "tir.noalias": True,
                "global_symbol": "default_function",
                "tir.is_entry_func": True,
                "calling_conv": 1,
            }
        )
        # body
        stack_tcode_data: T.Ptr[T.int32] = T.tvm_stack_alloca("arg_tcode", 10, dtype="handle")
        stack_tcode = T.buffer_decl([9], "int32", data=stack_tcode_data)
        stack_value: T.handle = T.tvm_stack_alloca("arg_value", 10, dtype="handle")
        assert num_args == 3, "default_function: num_args should be 3"
        arg0: T.handle = T.tvm_struct_get(args, 0, 12, dtype="handle")
        arg0_code: T.int32 = arg_type_ids[0]
        arg1: T.handle = T.tvm_struct_get(args, 1, 12, dtype="handle")
        arg1_code: T.int32 = arg_type_ids[1]
        arg2: T.handle = T.tvm_struct_get(args, 2, 12, dtype="handle")
        arg2_code: T.int32 = arg_type_ids[2]

        A: T.handle = T.tvm_struct_get(arg0, 0, 1, dtype="handle")
        T.attr(A, "storage_alignment", 128)
        arg0_shape_data: T.Ptr[T.int64] = T.tvm_struct_get(arg0, 0, 2, dtype="handle")
        arg0_shape = T.buffer_decl([6], "int64", data=arg0_shape_data)
        arg0_strides_data: T.Ptr[T.int64] = T.tvm_struct_get(arg0, 0, 3, dtype="handle")
        arg0_strides = T.buffer_decl([6], "int64", data=arg0_strides_data)

        dev_id: T.int32 = T.tvm_struct_get(arg0, 0, 9, dtype="int32")

        W: T.handle = T.tvm_struct_get(arg1, 0, 1, dtype="handle")
        T.attr(W, "storage_alignment", 128)
        arg1_shape_data: T.Ptr[T.int64] = T.tvm_struct_get(arg1, 0, 2, dtype="handle")
        arg1_shape = T.buffer_decl([6], "int64", data=arg1_shape_data)
        arg1_strides_data: T.Ptr[T.int64] = T.tvm_struct_get(arg1, 0, 3, dtype="handle")
        arg1_strides = T.buffer_decl([6], "int64", data=arg1_strides_data)

        Conv: T.handle = T.tvm_struct_get(arg2, 0, 1, dtype="handle")
        T.attr(Conv, "storage_alignment", 128)
        arg2_shape_data: T.Ptr[T.int64] = T.tvm_struct_get(arg2, 0, 2, dtype="handle")
        arg2_shape = T.buffer_decl([6], "int64", data=arg2_shape_data)
        arg2_strides_data: T.Ptr[T.int64] = T.tvm_struct_get(arg2, 0, 3, dtype="handle")
        arg2_strides = T.buffer_decl([6], "int64", data=arg2_strides_data)

        assert (((arg0_code == 3) or (arg0_code == 13)) or (arg0_code == 7)) or (
            arg0_code == 4
        ), "default_function: Expect arg[0] to be pointer"
        assert (((arg1_code == 3) or (arg1_code == 13)) or (arg1_code == 7)) or (
            arg1_code == 4
        ), "default_function: Expect arg[1] to be pointer"
        assert (((arg2_code == 3) or (arg2_code == 13)) or (arg2_code == 7)) or (
            arg2_code == 4
        ), "default_function: Expect arg[2] to be pointer"
        assert 6 == T.tvm_struct_get(arg0, 0, 4, dtype="int32"), "arg0.ndim is expected to equal 6"
        assert 6 == T.tvm_struct_get(arg0, 0, 4, dtype="int32"), "arg0.ndim is expected to equal 6"
        assert (
            (T.tvm_struct_get(arg0, 0, 5, dtype="uint8") == T.uint8(2))
            and (T.tvm_struct_get(arg0, 0, 6, dtype="uint8") == T.uint8(16))
        ) and (
            T.tvm_struct_get(arg0, 0, 7, dtype="uint16") == T.uint16(1)
        ), "arg0.dtype is expected to be float16"
        assert 16 == T.cast(
            arg0_shape[0], "int32"
        ), "Argument arg0.shape[0] has an unsatisfied constraint"
        assert 14 == T.cast(
            arg0_shape[1], "int32"
        ), "Argument arg0.shape[1] has an unsatisfied constraint"
        assert 14 == T.cast(
            arg0_shape[2], "int32"
        ), "Argument arg0.shape[2] has an unsatisfied constraint"
        assert 16 == T.cast(
            arg0_shape[3], "int32"
        ), "Argument arg0.shape[3] has an unsatisfied constraint"
        assert 16 == T.cast(
            arg0_shape[4], "int32"
        ), "Argument arg0.shape[4] has an unsatisfied constraint"
        assert 16 == T.cast(
            arg0_shape[5], "int32"
        ), "Argument arg0.shape[5] has an unsatisfied constraint"
        if not (T.isnullptr(arg0_strides.data, dtype="bool")):
            assert (
                (
                    (
                        (
                            (1 == T.cast(arg0_strides[5], "int32"))
                            and (16 == T.cast(arg0_strides[4], "int32"))
                        )
                        and (256 == T.cast(arg0_strides[3], "int32"))
                    )
                    and (4096 == T.cast(arg0_strides[2], "int32"))
                )
                and (57344 == T.cast(arg0_strides[1], "int32"))
            ) and (
                802816 == T.cast(arg0_strides[0], "int32")
            ), "arg0.strides: expected to be compact array"
            T.evaluate(0)
        assert T.uint64(0) == T.tvm_struct_get(
            arg0, 0, 8, dtype="uint64"
        ), "Argument arg0.byte_offset has an unsatisfied constraint"
        assert 2 == T.tvm_struct_get(
            arg0, 0, 10, dtype="int32"
        ), "Argument arg0.device_type has an unsatisfied constraint"
        assert 6 == T.tvm_struct_get(arg1, 0, 4, dtype="int32"), "arg1.ndim is expected to equal 6"
        assert 6 == T.tvm_struct_get(arg1, 0, 4, dtype="int32"), "arg1.ndim is expected to equal 6"
        assert (
            (T.tvm_struct_get(arg1, 0, 5, dtype="uint8") == T.uint8(2))
            and (T.tvm_struct_get(arg1, 0, 6, dtype="uint8") == T.uint8(16))
        ) and (
            T.tvm_struct_get(arg1, 0, 7, dtype="uint16") == T.uint16(1)
        ), "arg1.dtype is expected to be float16"
        assert 3 == T.cast(
            arg1_shape[0], "int32"
        ), "Argument arg1.shape[0] has an unsatisfied constraint"
        assert 3 == T.cast(
            arg1_shape[1], "int32"
        ), "Argument arg1.shape[1] has an unsatisfied constraint"
        assert 16 == T.cast(
            arg1_shape[2], "int32"
        ), "Argument arg1.shape[2] has an unsatisfied constraint"
        assert 32 == T.cast(
            arg1_shape[3], "int32"
        ), "Argument arg1.shape[3] has an unsatisfied constraint"
        assert 16 == T.cast(
            arg1_shape[4], "int32"
        ), "Argument arg1.shape[4] has an unsatisfied constraint"
        assert 16 == T.cast(
            arg1_shape[5], "int32"
        ), "Argument arg1.shape[5] has an unsatisfied constraint"
        if not (T.isnullptr(arg1_strides.data, dtype="bool")):
            assert (
                (
                    (
                        (
                            (1 == T.cast(arg1_strides[5], "int32"))
                            and (16 == T.cast(arg1_strides[4], "int32"))
                        )
                        and (256 == T.cast(arg1_strides[3], "int32"))
                    )
                    and (8192 == T.cast(arg1_strides[2], "int32"))
                )
                and (131072 == T.cast(arg1_strides[1], "int32"))
            ) and (
                393216 == T.cast(arg1_strides[0], "int32")
            ), "arg1.strides: expected to be compact array"
            T.evaluate(0)
        assert T.uint64(0) == T.tvm_struct_get(
            arg1, 0, 8, dtype="uint64"
        ), "Argument arg1.byte_offset has an unsatisfied constraint"
        assert 2 == T.tvm_struct_get(
            arg1, 0, 10, dtype="int32"
        ), "Argument arg1.device_type has an unsatisfied constraint"
        assert dev_id == T.tvm_struct_get(
            arg1, 0, 9, dtype="int32"
        ), "Argument arg1.device_id has an unsatisfied constraint"
        assert 6 == T.tvm_struct_get(arg2, 0, 4, dtype="int32"), "arg2.ndim is expected to equal 6"
        assert 6 == T.tvm_struct_get(arg2, 0, 4, dtype="int32"), "arg2.ndim is expected to equal 6"
        assert (
            (T.tvm_struct_get(arg2, 0, 5, dtype="uint8") == T.uint8(2))
            and (T.tvm_struct_get(arg2, 0, 6, dtype="uint8") == T.uint8(32))
        ) and (
            T.tvm_struct_get(arg2, 0, 7, dtype="uint16") == T.uint16(1)
        ), "arg2.dtype is expected to be float32"
        assert 16 == T.cast(
            arg2_shape[0], "int32"
        ), "Argument arg2.shape[0] has an unsatisfied constraint"
        assert 14 == T.cast(
            arg2_shape[1], "int32"
        ), "Argument arg2.shape[1] has an unsatisfied constraint"
        assert 14 == T.cast(
            arg2_shape[2], "int32"
        ), "Argument arg2.shape[2] has an unsatisfied constraint"
        assert 32 == T.cast(
            arg2_shape[3], "int32"
        ), "Argument arg2.shape[3] has an unsatisfied constraint"
        assert 16 == T.cast(
            arg2_shape[4], "int32"
        ), "Argument arg2.shape[4] has an unsatisfied constraint"
        assert 16 == T.cast(
            arg2_shape[5], "int32"
        ), "Argument arg2.shape[5] has an unsatisfied constraint"
        if not (T.isnullptr(arg2_strides.data, dtype="bool")):
            assert (
                (
                    (
                        (
                            (1 == T.cast(arg2_strides[5], "int32"))
                            and (16 == T.cast(arg2_strides[4], "int32"))
                        )
                        and (256 == T.cast(arg2_strides[3], "int32"))
                    )
                    and (8192 == T.cast(arg2_strides[2], "int32"))
                )
                and (114688 == T.cast(arg2_strides[1], "int32"))
            ) and (
                1605632 == T.cast(arg2_strides[0], "int32")
            ), "arg2.strides: expected to be compact array"
            T.evaluate(0)
        assert T.uint64(0) == T.tvm_struct_get(
            arg2, 0, 8, dtype="uint64"
        ), "Argument arg2.byte_offset has an unsatisfied constraint"
        assert 2 == T.tvm_struct_get(
            arg2, 0, 10, dtype="int32"
        ), "Argument arg2.device_type has an unsatisfied constraint"
        assert dev_id == T.tvm_struct_get(
            arg2, 0, 9, dtype="int32"
        ), "Argument arg2.device_id has an unsatisfied constraint"
        T.evaluate(T.tvm_struct_set(stack_value, 0, 12, T.cast(2, "int64"), dtype="int32"))
        stack_tcode[0] = 0
        T.evaluate(T.tvm_struct_set(stack_value, 1, 12, T.cast(dev_id, "int64"), dtype="int32"))
        stack_tcode[1] = 0
        T.evaluate(
            T.tvm_call_packed_lowered(
                "__tvm_set_device", stack_value, stack_tcode.data, 0, 2, dtype="int32"
            )
        )
        T.attr(0, "compute_scope", "default_function_compute_")
        T.evaluate(T.tvm_struct_set(stack_value, 0, 12, A, dtype="int32"))
        stack_tcode[0] = 3
        T.evaluate(T.tvm_struct_set(stack_value, 1, 12, W, dtype="int32"))
        stack_tcode[1] = 3
        T.evaluate(T.tvm_struct_set(stack_value, 2, 12, Conv, dtype="int32"))
        stack_tcode[2] = 3
        T.evaluate(T.tvm_struct_set(stack_value, 3, 12, T.cast(196, "int64"), dtype="int32"))
        stack_tcode[3] = 0
        T.evaluate(T.tvm_struct_set(stack_value, 4, 12, T.cast(2, "int64"), dtype="int32"))
        stack_tcode[4] = 0
        T.evaluate(T.tvm_struct_set(stack_value, 5, 12, T.cast(4, "int64"), dtype="int32"))
        stack_tcode[5] = 0
        T.evaluate(T.tvm_struct_set(stack_value, 6, 12, T.cast(4, "int64"), dtype="int32"))
        stack_tcode[6] = 0
        T.evaluate(T.tvm_struct_set(stack_value, 7, 12, T.cast(2, "int64"), dtype="int32"))
        stack_tcode[7] = 0
        T.evaluate(T.tvm_struct_set(stack_value, 8, 12, T.cast(32, "int64"), dtype="int32"))
        stack_tcode[8] = 0
        T.evaluate(
            T.tvm_call_packed_lowered(
                "default_function_kernel0", stack_value, stack_tcode.data, 0, 9, dtype="int32"
            )
        )

    return opt_conv_tensorcore_mod_host


def vthread_func():
    @T.prim_func
    def vthread_func(a: T.handle, c: T.handle) -> None:
        A = T.match_buffer(a, [256], "float32")
        C = T.match_buffer(c, [256], "float32")

        i0 = T.env_thread("blockIdx.x")
        i1 = T.env_thread("threadIdx.x")
        i2 = T.env_thread("vthread")

        T.launch_thread(i0, 4)
        T.launch_thread(i1, 2)
        T.launch_thread(i2, 2)
        B = T.allocate([16], "float32", "local")
        for j in range(16):
            B[j] = A[i0 * 64 + i1 * 32 + i2 * 16 + j] + T.float32(1)
        for j in range(16):
            C[i0 * 64 + i1 * 32 + i2 * 16 + j] = B[j] * T.float32(2)

    return vthread_func


def matmul():
    @T.prim_func
    def matmul(a: T.handle, b: T.handle, c: T.handle) -> None:
        A = T.match_buffer(a, [128, 128])
        B = T.match_buffer(b, [128, 128])
        C = T.match_buffer(c, [128, 128])

        for i, j, k in T.grid(128, 128, 128):
            with T.block("update"):
                vi, vj, vk = T.axis.remap("SSR", [i, j, k])
                with T.init():
                    C[vi, vj] = T.float32(0)
                C[vi, vj] = C[vi, vj] + A[vi, vk] * B[vj, vk]

    return matmul


def matmul_original():
    @T.prim_func
    def matmul_original(a: T.handle, b: T.handle, c: T.handle) -> None:
        A = T.match_buffer(a, [128, 128])
        B = T.match_buffer(b, [128, 128])
        C = T.match_buffer(c, [128, 128])

        for i, j in T.grid(128, 128):
            with T.block("init"):
                vi, vj = T.axis.remap("SS", [i, j])
                C[vi, vj] = T.float32(0)

            for k in range(128):
                with T.block("update"):
                    vi, vj, vk = T.axis.remap("SSR", [i, j, k])
                    C[vi, vj] = C[vi, vj] + A[vi, vk] * B[vj, vk]

    return matmul_original


def element_wise():
    @T.prim_func
    def element_wise(a: T.handle, c: T.handle) -> None:
        A = T.match_buffer(a, (128, 128), "float32")
        C = T.match_buffer(c, (128, 128), "float32")
        B = T.alloc_buffer((128, 128), "float32")

        for i, j in T.grid(128, 128):
            with T.block("B"):
                vi, vj = T.axis.remap("SS", [i, j])
                B[vi, vj] = A[vi, vj] * T.float32(2)
        for i, j in T.grid(128, 128):
            with T.block("C"):
                vi, vj = T.axis.remap("SS", [i, j])
                C[vi, vj] = B[vi, vj] + T.float32(1)

    return element_wise


def predicate():
    @T.prim_func
    def predicate(b: T.handle, c: T.handle) -> None:
        B = T.match_buffer(b, (16, 16), "float32")
        C = T.match_buffer(c, (16, 16), "float32")

        for i, jo, ji in T.grid(16, 4, 5):
            with T.block("update"):
                vi = T.axis.S(16, i)
                vj = T.axis.S(16, jo * 4 + ji)
                T.where(jo * 4 + ji < 16)
                C[vi, vj] = B[vi, vj] + T.float32(1)

    return predicate


def test_module_define():
    func1 = tvm.ir.IRModule({"matmul": matmul()})["matmul"]
    func2 = tvm.ir.IRModule({"element_wise": element_wise()})["element_wise"]
    func3 = tvm.ir.IRModule({"predicate": predicate()})["predicate"]
    mod1 = tvm.ir.IRModule({"func1": func1, "func2": func2, "func3": func3})
    mod2 = tvm.ir.IRModule({"func1": matmul(), "func2": element_wise(), "func3": predicate()})
    tvm.ir.assert_structural_equal(mod1, mod2)


def test_matmul_original():
    func = matmul_original()
    rt_func = tvm.script.from_source(func.script(show_meta=True))
    tvm.ir.assert_structural_equal(func, rt_func)

    assert isinstance(rt_func.body.block, tir.stmt.Block)
    assert isinstance(rt_func.body.block.body, tir.stmt.For)
    assert isinstance(rt_func.body.block.body.body, tir.stmt.For)
    assert isinstance(rt_func.body.block.body.body.body, tir.stmt.SeqStmt)
    assert isinstance(rt_func.body.block.body.body.body[0].block, tir.stmt.Block)
    assert isinstance(rt_func.body.block.body.body.body[1], tir.stmt.For)
    assert isinstance(rt_func.body.block.body.body.body[1].body.block, tir.stmt.Block)


def test_element_wise():
    func = element_wise()
    rt_func = tvm.script.from_source(func.script(show_meta=True))
    tvm.ir.assert_structural_equal(func, rt_func)

    assert isinstance(rt_func.body.block, tir.stmt.Block)
    assert isinstance(rt_func.body.block.body, tir.stmt.SeqStmt)
    assert isinstance(rt_func.body.block.body[0], tir.stmt.For)
    assert isinstance(rt_func.body.block.body[0].body, tir.stmt.For)
    assert isinstance(rt_func.body.block.body[0].body.body.block, tir.stmt.Block)

    assert isinstance(rt_func.body.block.body[1], tir.stmt.For)
    assert isinstance(rt_func.body.block.body[1].body, tir.stmt.For)
    assert isinstance(rt_func.body.block.body[1].body.body.block, tir.stmt.Block)


def test_predicate():
    func = predicate()
    rt_func = tvm.script.from_source(func.script(show_meta=True))
    tvm.ir.assert_structural_equal(func, rt_func)

    assert isinstance(rt_func.body.block, tir.stmt.Block)
    assert isinstance(rt_func.body.block.body, tir.stmt.For)
    assert isinstance(rt_func.body.block.body.body, tir.stmt.For)
    assert isinstance(rt_func.body.block.body.body.body, tir.stmt.For)
    assert isinstance(rt_func.body.block.body.body.body.body.block, tir.stmt.Block)


def for_thread_binding():
    @T.prim_func
    def for_thread_binding(a: T.handle, b: T.handle) -> None:
        A = T.match_buffer(a, (16, 16), "float32")
        B = T.match_buffer(b, (16, 16), "float32")

        for i in T.thread_binding(0, 16, thread="threadIdx.x"):
            for j in T.thread_binding(
                0, 16, thread="threadIdx.y", annotations={"attr_key": "attr_value"}
            ):
                A[i, j] = B[i, j] + T.float32(1)

    return for_thread_binding


def test_for_thread_binding():
    func = for_thread_binding()
    rt_func = tvm.script.from_source(func.script(show_meta=True))
    tvm.ir.assert_structural_equal(func, rt_func)

    assert isinstance(rt_func.body, tir.stmt.For)
    assert rt_func.body.kind == 4
    assert rt_func.body.thread_binding.thread_tag == "threadIdx.x"
    assert isinstance(rt_func.body.body, tir.stmt.For)
    assert rt_func.body.body.kind == 4
    assert rt_func.body.body.thread_binding.thread_tag == "threadIdx.y"
    assert rt_func.body.body.annotations["attr_key"] == "attr_value"


def match_buffer_region():
    @T.prim_func
    def match_buffer_region(a: T.handle, b: T.handle) -> None:
        A = T.match_buffer(a, (16, 16, 16), "float32")
        B = T.match_buffer(b, (1), "float32")

        for i, j in T.grid(16, 4):
            with T.block():
                vi, vj = T.axis.remap("SS", [i, j])
                C = T.match_buffer(A[0:16, vi, vj * 4 : vj * 4 + 4], (16, 1, 4))
                for ii in range(4):
                    with T.block():
                        vii = T.axis.S(4, ii)
                        D = T.match_buffer(C[vii * 4 : vii * 4 + 4, 0, 0:4], (4, 1, 4))
                        for i, j in T.grid(4, 4):
                            B[0] += D[i, 0, j]

    return match_buffer_region


def test_match_buffer_region():
    func = match_buffer_region()
    rt_func = tvm.script.from_source(func.script(show_meta=True))
    tvm.ir.assert_structural_equal(func, rt_func)

    assert isinstance(rt_func.body, tir.stmt.BlockRealize)
    root = rt_func.body.block

    assert isinstance(root.body, tir.stmt.For)
    assert isinstance(root.body.body, tir.stmt.For)
    assert isinstance(root.body.body.body, tir.stmt.BlockRealize)
    outer_block = root.body.body.body.block
    assert len(outer_block.match_buffers) == 1
    buffer_C = outer_block.match_buffers[0].buffer
    tvm.ir.assert_structural_equal(buffer_C.shape, [16, 1, 4])

    assert isinstance(outer_block.body, tir.stmt.For)
    assert isinstance(outer_block.body.body, tir.stmt.BlockRealize)
    inner_block = outer_block.body.body.block
    assert len(inner_block.match_buffers) == 1
    buffer_D = inner_block.match_buffers[0].buffer
    tvm.ir.assert_structural_equal(buffer_D.shape, [4, 1, 4])


def block_elements():
    @T.prim_func
    def block_elements(a: T.handle, b: T.handle) -> None:
        A = T.match_buffer(a, (16, 16), "float32")
        B = T.match_buffer(b, (1, 1), "float32")

        with T.block("update"):
            vi = T.axis.S(1, 0)
            T.where(True)
            T.reads(A[0:16, 0:16])
            T.writes(B[0, 0])
            T.block_attr({"attr_key": "attr_value"})
            C = T.alloc_buffer((4, 4), dtype="float32")
            D = T.match_buffer(A[0:4, 0], (4, 1))
            with T.init():
                B[0, 0] = T.float32(0)
            B[0, 0] = A[0, 0] + B[0, 0] + C[1, 1] + D[2, 0]

    return block_elements


def test_block_elements():
    func = block_elements()
    rt_func = tvm.script.from_source(func.script(show_meta=True))
    tvm.ir.assert_structural_equal(func, rt_func)

    assert isinstance(rt_func.body.block, tir.stmt.Block)
    assert isinstance(rt_func.body.block.body, tir.stmt.BlockRealize)
    assert isinstance(rt_func.body.block.body.block, tir.stmt.Block)
    block = rt_func.body.block.body.block
    assert isinstance(block.body, tir.stmt.BufferStore)
    assert isinstance(block.init, tir.stmt.BufferStore)
    assert len(block.annotations) == 1
    assert block.annotations["attr_key"] == "attr_value"


def opaque_block():
    @T.prim_func
    def opaque_block(a: T.handle, b: T.handle) -> None:
        A = T.match_buffer(a, (16, 16), "float32")
        B = T.match_buffer(b, (16, 16), "float32")

        for i in range(16):
            for j in range(16):
                with T.block():
                    T.reads([])
                    T.writes(A[i, j])
                    A[i, j] = T.float32(0)
            with T.block():
                T.reads([A[i, 0:16]])
                T.writes([B[i, 0:16]])
                for j in range(16):
                    B[i, j] = A[i, j]

    return opaque_block


def test_opaque_block():
    func = opaque_block()
    rt_func = tvm.script.from_source(func.script(show_meta=True))
    tvm.ir.assert_structural_equal(func, rt_func)

    root_block = rt_func.body.block
    assert isinstance(root_block, tir.stmt.Block)
    assert isinstance(root_block.body, tir.stmt.For)
    assert isinstance(root_block.body.body[0], tir.stmt.For)
    assert isinstance(root_block.body.body[0].body, tir.stmt.BlockRealize)
    assert isinstance(root_block.body.body[0].body.block, tir.stmt.Block)
    assert len(root_block.body.body[0].body.block.iter_vars) == 0
    assert isinstance(root_block.body.body[1], tir.stmt.BlockRealize)
    assert isinstance(root_block.body.body[1].block, tir.stmt.Block)
    assert len(root_block.body.body[1].block.iter_vars) == 0


def module_const():
    @tvm.script.ir_module
    class Module4:
        # There is an ongoing (python)dict->(c++)Map->(python)dict issue which potentially
        # changes order of the items in dict after roundtrip due to map not support order
        # of insertion while dict does. Hence func 'def A(a: T.handle, c: T.handle) -> None'
        # is commented
        #
        #  test:
        #  d = {"B": 1, "A": 2}
        #  m = tvm.runtime.convert(d)
        #  assert d.keys() == m.keys(), f"Order changed from {list(d.keys())} to {list(m.keys())}"

        """
        @T.prim_func
        def A(a: T.handle, c: T.handle) -> None:
            A = T.match_buffer(a, (10), "int32")
            C = T.match_buffer(c, (10), "int32")
            B = T.alloc_buffer((10), "int32")

            K1 = T.allocate_const([1, 1, 1, 1, 1, 1, 1, 1, 1, 1], "int32", [10])
            for x in T.serial(0, 10):
                B[x] = A[x] + T.load("int32", K1, x)

            for x in T.serial(0, 10):
                C[x] = B[x]
        """

        @T.prim_func
        def B(a: T.handle, c: T.handle) -> None:
            A = T.match_buffer(a, (10), "int32")
            C = T.match_buffer(c, (10), "int32")
            B = T.alloc_buffer((10), "int32")

            K1 = T.allocate_const([1, 1, 1, 1, 1, 1, 1, 1, 1, 1], "int32", [10])
            for x in T.serial(0, 10):
                B[x] = A[x] + K1[x]

            K2 = T.allocate_const([1, 1, 1, 1, 1, 1, 1, 1, 1, 1], "int32", [10])
            for x in T.serial(0, 10):
                B[x] = B[x] + K2[x]

            for x in T.serial(0, 10):
                C[x] = B[x]

    return Module4


def constant():
    @T.prim_func
    def constant(a: T.handle, c: T.handle) -> None:
        A = T.match_buffer(a, (10), "int32")
        C = T.match_buffer(c, (10), "int32")
        B = T.alloc_buffer((10), "int32")
        K = T.allocate_const([1, 1, 1, 1, 1, 1, 1, 1, 1, 1], "int32", [10])
        for x in T.serial(0, 10):
            B[x] = A[x] + K[x]

        for x in T.serial(0, 10):
            C[x] = B[x]

    return constant


def rank0():
    @T.prim_func
    def rank0(a: T.handle) -> None:
        A = T.match_buffer(a, (), "float32")
        B = T.alloc_buffer((), "float32")
        A[()] = 2
        B[()] = A[()]

    return rank0


def rank0_block():
    @T.prim_func
    def rank0_block(a: T.handle) -> None:
        A = T.match_buffer(a, (), "float32")
        B = T.alloc_buffer((), "float32")
        B[()] = A[()]

        with T.block("update"):
            T.reads([A[()]])
            T.writes([B[()]])
            for i in range(1):
                B[()] = A[()]

    return rank0_block


def select():
    @T.prim_func
    def select(a: T.handle) -> None:
        A = T.match_buffer(a, (), "float32")
        A[()] = T.Select(True, 1, 2)

    return select


def minmax():
    @T.prim_func
    def minmax(a: T.handle) -> None:
        A = T.match_buffer(a, (), "float32")
        A[()] = T.min(1, 2)
        A[()] = T.max(1, 2)

    return minmax


def abs():
    @T.prim_func
    def abs(a: T.handle) -> None:
        A = T.match_buffer(a, (128, 128), "float32")

        for i, j in T.grid(128, 128):
            with T.block("A"):
                vi, vj = T.axis.remap("SS", [i, j])
                A[vi, vj] = T.abs(A[vi, vj])

    return abs


def constant_folding():
    @T.prim_func
    def constant_folding(a: T.handle) -> None:
        A = T.match_buffer(a, (), "float32")
        A[()] = T.min(2.2, 5.2)
        A[()] = T.max(T.float32(2.2), T.float32(T.float32(5.2)))
        A[()] = T.min(2.2, 5.0)

    return constant_folding


def simplify_bracket():
    @T.prim_func
    def simplify_bracket() -> None:
        a = T.var("int32")
        b = T.var("int32")
        c = T.var("int32")
        d = T.var("int32")
        T.evaluate(a + b * (c + d))

    return simplify_bracket


def var_with_same_name():
    @T.prim_func
    def var_with_same_name(a: T.handle) -> None:
        A = T.match_buffer(a, (16, 16), "float32")
        for i, j in T.grid(16, 16):
            with T.block():
                vi, vj = T.axis.remap("SS", [i, j])
                A[vi, vj] = 0
        for i, j in T.grid(16, 16):
            with T.block():
                vi, vj = T.axis.remap("SS", [i, j])
                A[vi, vj] = 0

    return var_with_same_name


def test_same_name_var():
    func = var_with_same_name()
    out_str = func.script(tir_prefix="T", show_meta=True)
    rt_func = tvm.script.from_source(out_str)
    tvm.ir.assert_structural_equal(func, rt_func)

    assert out_str.count('vi, vj = T.axis.remap("SS", [i, j])') == 2
    assert out_str.find("vi_") == -1
    assert out_str.find("vj_") == -1

    assert out_str.count("for i, j in T.grid(16, 16)") == 2
    assert out_str.find("i_") == -1
    assert out_str.find("i_") == -1


def while_loop():
    @T.prim_func
    def while_loop(a: T.handle, b: T.handle) -> None:
        A = T.match_buffer(a, (16,), "float32")
        B = T.match_buffer(b, (16,), "float32")
        i = T.alloc_buffer((), "int32", scope="local")
        for ii in range(16):
            with T.block():
                vi = T.axis.S(16, ii)
                B[vi] = 0
            while i[()] < 10:
                for j in range(16):
                    B[j] += A[j]

    return while_loop


# fmt: off
def primfunc_with_allocate_annotations():
    @T.prim_func
    def primfunc_with_allocate_annotations(placeholder_28: T.handle, T_cast_6: T.handle) -> None:
        # function attr dict
        T.func_attr({"global_symbol": "tvmgen_default_fused_nn_max_pool2d_cast", "tir.noalias": True})
        placeholder_29 = T.match_buffer(placeholder_28, [802816], dtype="uint8", elem_offset=0, align=64, offset_factor=1)
        T_cast_7 = T.match_buffer(T_cast_6, [200704], dtype="int16", elem_offset=0, align=64, offset_factor=1)
        # body
        tensor_2 = T.allocate([200704], "uint8", "global", annotations={"attr1_key": "attr1_value"})
        for ax0_ax1_fused_4 in T.serial(0, 56):
            for ax2_4 in T.serial(0, 56):
                for ax3_init in T.serial(0, 64):
                    tensor_2[(((ax0_ax1_fused_4*3584) + (ax2_4*64)) + ax3_init)] = T.uint8(0)
                for rv0_rv1_fused_1, ax3_2 in T.grid(9, 64):
                    tensor_2[(((ax0_ax1_fused_4*3584) + (ax2_4*64)) + ax3_2)] = T.max(tensor_2[(((ax0_ax1_fused_4*3584) + (ax2_4*64)) + ax3_2)], T.if_then_else(((((ax0_ax1_fused_4*2) + T.floordiv(rv0_rv1_fused_1, 3)) < 112) and (((ax2_4*2) + T.floormod(rv0_rv1_fused_1, 3)) < 112)), placeholder_29[(((((ax0_ax1_fused_4*14336) + (T.floordiv(rv0_rv1_fused_1, 3)*7168)) + (ax2_4*128)) + (T.floormod(rv0_rv1_fused_1, 3)*64)) + ax3_2)], T.uint8(0), dtype="uint8"))
        for ax0_ax1_fused_5 in T.serial(0, 56):
            for ax2_5, ax3_3 in T.grid(56, 64):
                T_cast_7[(((ax0_ax1_fused_5*3584) + (ax2_5*64)) + ax3_3)] = T.cast(tensor_2[(((ax0_ax1_fused_5*3584) + (ax2_5*64)) + ax3_3)], "int16")

    return primfunc_with_allocate_annotations
# fmt: on




# fmt: off
def comm_reducer_single_reduce_group():
    @T.prim_func
    def comm_reducer_single_reduce_group(a: T.handle, b: T.handle) -> None:
        T.func_attr({"global_symbol": "main", "tir.noalias": True})
        threadIdx_x = T.env_thread("threadIdx.x")
        A = T.match_buffer(a, [128 * 128], dtype="float32")
        for i in T.serial(0, 128):
            T.launch_thread(threadIdx_x, 128)
            reduce_temp0 = T.allocate([1], "float32", "local")
            with T.attr(T.comm_reducer(lambda x, y: x + y, [T.float32(0)]), "reduce_scope", T.reinterpret(T.uint64(0), dtype="handle")):
                T.evaluate(T.tvm_thread_allreduce(T.uint32(1), A[i * 128 + threadIdx_x], True, reduce_temp0.data, threadIdx_x, dtype="handle"))

    return comm_reducer_single_reduce_group


def comm_reducer_multiple_reduce_groups():
    @T.prim_func
    def comm_reducer_multiple_reduce_groups(a: T.handle, b: T.handle) -> None:
        T.func_attr({"global_symbol": "main", "tir.noalias": True})
        threadIdx_x = T.env_thread("threadIdx.x")
        A = T.match_buffer(a, [128 * 128], dtype="float32")
        for i in T.serial(0, 128):
            T.launch_thread(threadIdx_x, 128)
            reduce_temp0 = T.allocate([1], "float32", "local")
            with T.attr(T.comm_reducer(lambda x0, x1, y0, y1: (T.Select((x1 >= y1), x0, y0), T.Select((x1 >= y1), x1, y1)), [T.int32(-1), T.min_value("float32")]), "reduce_scope", T.reinterpret(T.uint64(0), dtype="handle")):
                T.evaluate(T.tvm_thread_allreduce(T.uint32(1), A[i * 128 + threadIdx_x], True, reduce_temp0.data, threadIdx_x, dtype="handle"))

    return comm_reducer_multiple_reduce_groups


def multiple_commreducer():
    @T.prim_func
    def multiple_commreducer() -> None:
        normal_reduce_temp0 = T.buffer_decl([1], dtype="float32", strides=[1], scope="local")
        normal_reduce_temp1 = T.buffer_decl([1], dtype="float32", strides=[1], scope="local")
        reduce_temp0 = T.buffer_decl([1], dtype="float32", strides=[1], scope="local")
        reduce_temp1 = T.buffer_decl([1], dtype="float32", strides=[1], scope="local")
        for ax0_1 in T.thread_binding(0, 32, thread="threadIdx.x"):
            with T.block("T_softmax_maxelem_cross_thread_reduction"):
                T.attr(T.comm_reducer(lambda x, y: T.max(x, y), [T.min_value("float32")]), "reduce_scope", T.reinterpret(T.uint64(0), dtype="handle"))
                T.evaluate(T.tvm_thread_allreduce(T.uint32(1), normal_reduce_temp0[0], True, reduce_temp0.data, ax0_1, dtype="handle"))
        for ax0_1 in T.thread_binding(0, 32, thread="threadIdx.x"):
            with T.block("T_softmax_expsum_cross_thread_reduction"):
                T.attr(T.comm_reducer(lambda x, y: x + y, [T.float32(0)]), "reduce_scope", T.reinterpret(T.uint64(0), dtype="handle"))
                T.evaluate(T.tvm_thread_allreduce(T.uint32(1), normal_reduce_temp1[0], True, reduce_temp1.data, ax0_1, dtype="handle"))

    return multiple_commreducer
# fmt: on


def func_div_mod():
    @T.prim_func
    def func_div_mod():
        a = T.var("int32")
        b = T.var("int32")
        T.evaluate(a // b)
        T.evaluate(a % b)
        T.evaluate(T.truncmod(a, b))

    return func_div_mod


def test_div_mod():
    func = func_div_mod()
    rt_func = tvm.script.from_source(func.script())
    tvm.ir.assert_structural_equal(func, rt_func, True)

    assert isinstance(func.body[0].value, tvm.tir.FloorDiv)
    assert isinstance(func.body[1].value, tvm.tir.FloorMod)
    assert isinstance(func.body[2].value, tvm.tir.Mod)


def loop_extent_dependent():
    @T.prim_func
    def loop_extent_dependent(a: T.handle) -> None:
        A = T.match_buffer(a, [], dtype="int32")
        for i in T.serial(0, 128):
            for j in T.serial(0, i):
                A[()] = A[()] + j

    return loop_extent_dependent


def nontrivial_range_axis():
    @T.prim_func
    def nontrivial_range_axis(a: T.handle) -> None:
        A = T.match_buffer(a, (10), "float32")
        for i in range(10):
            with T.block("block"):
                vi = T.axis.spatial((1, 11), i + 1)
                A[vi - 1] = A[vi - 1] + 1.0

    return nontrivial_range_axis


def func_with_target_spec_by_config():
    @T.prim_func
    def func_with_target_spec_by_config() -> None:
        T.func_attr(
            {
                "kTarget": T.target(
                    {
                        "max_num_threads": 1024,
                        "arch": "sm_70",
                        "thread_warp_size": 32,
                        "kind": "cuda",
                        "tag": "",
                        "keys": ["cuda", "gpu"],
                        "host": T.target({"kind": "llvm", "tag": "", "keys": ["cpu"]}),
                    }
                )
            }
        )
        T.evaluate(0)

    return func_with_target_spec_by_config


def func_with_target_spec_by_str():
    @T.prim_func
    def func_with_target_spec_by_str() -> None:
        T.func_attr({"kTarget": T.target("nvidia/nvidia-a100")})
        T.evaluate(0)

    return func_with_target_spec_by_str


def func_root_attr():
    @T.prim_func
    def func_root_attr():
        with T.block("root"):
            T.block_attr({"a": "0"})
            T.evaluate(0)

    return func_root_attr


def func_T_ptr_let_statement():
    @T.prim_func
    def func_T_ptr_let_statement(
        args: T.handle, arg_type_ids_handle: T.Ptr[T.int32], num_args: T.int32
    ) -> None:
        # The T.Ptr declaration in the parameter list should parse
        # correctly, and should be usable as the data pointer in a buffer.
        arg_type_ids = T.buffer_decl([2], dtype="int32", data=arg_type_ids_handle)

        arg0: T.handle = T.tvm_struct_get(args, 0, 12, dtype="handle")
        arg1: T.handle = T.tvm_struct_get(args, 1, 12, dtype="handle")

        # Functions that return a "handle" can be assigned to a T.Ptr
        # variable.  A variable annotated with T.Ptr still has dtype of
        # T.handle, but has type annotation as a pointer type.
        A_data: T.Ptr[T.float32] = T.tvm_struct_get(arg0, 0, 1, dtype="handle")

        # The buffer declaration has a data pointer defined earlier in
        # this function.  It should only be defined after the data pointer
        # has been defined, and should not be hoisted into the header of
        # the function as other buffer_decl statements can be.
        A = T.buffer_decl([1024], dtype="float32", data=A_data)
        B_data: T.Ptr[T.float32] = T.tvm_struct_get(arg1, 0, 1, dtype="handle")
        B = T.buffer_decl([1024], dtype="float32", data=B_data)

        B[0] = A[0]

    return func_T_ptr_let_statement


def func_T_ptr_allocate():
    @T.prim_func
    def func_T_ptr_allocate() -> None:
        A = T.allocate([1024], "float32", "global")
        A[0] = 0.0

    return func_T_ptr_allocate


def llvm_intrin_call():
    @T.prim_func
    def ctpop(A: T.Buffer[(16,), "uint8"], B: T.Buffer[(16,), "uint8"]) -> None:
        for i in range(0, 16):
            with T.block("A"):
                vi = T.axis.remap(
                    "S",
                    [
                        i,
                    ],
                )
                B[vi] = T.call_llvm_pure_intrin(
                    T.llvm_lookup_intrinsic_id("llvm.ctpop.i8"),
                    T.uint32(1),
                    A[vi],
                    dtype="uint8",
                )

    return ctpop


def parse_bufferslice_as_range_bound():
    @T.prim_func
    def segment_sum(
        A_ptr: T.handle, B_ptr: T.handle, indptr_ptr: T.handle, n: T.int32, m: T.int32
    ) -> None:
        A = T.match_buffer(A_ptr, [m], dtype="float32")
        B = T.match_buffer(B_ptr, [n], dtype="float32")
        indptr = T.match_buffer(indptr_ptr, [n + 1], dtype="int32")
        for i in T.serial(n):
            with T.block("outer"):
                vi = T.axis.spatial(n, i)
                T.reads(indptr[i : i + 2], B[vi], A[indptr[i] : indptr[i + 1]])
                T.writes(B[vi])
                for j in T.serial(indptr[i], indptr[i + 1]):
                    with T.block("inner"):
                        vj = T.axis.reduce(m, j)
                        T.reads(B[vi], A[vj])
                        T.writes(B[vi])
                        with T.init():
                            B[vi] = T.float32(0)
                        B[vi] = B[vi] + A[vj]

    return segment_sum


def int64_support():
    @T.prim_func
    def elementwise_shape_int64(a: T.handle, c: T.handle) -> None:
        A = T.match_buffer(a, (T.int64(128), T.int64(128)), dtype="float32")
        B = T.alloc_buffer((T.int64(128), T.int64(128)), dtype="float32")
        C = T.match_buffer(c, (T.int64(128), T.int64(128)), dtype="float32")
        for i, j in T.grid(128, 128):
            with T.block("B"):
                vi, vj = T.axis.remap("SS", [i, j])
                B[vi, vj] = A[vi, vj] * 2.0
        for i, j in T.grid(T.int64(128), T.int64(128)):
            with T.block("C"):
                vi, vj = T.axis.remap("SS", [i, j])
                C[vi, vj] = B[vi, vj] + 1.0

    return elementwise_shape_int64


def string_annotation_escaping():
    @T.prim_func
    def string_annotation_of_special_chars():
        T.func_attr(
            {
                "key1": '"\'hello\t\r"',
                "key2": """
            %1 = add i32 %0, %0
            %2 = add i32 %0, %1
            %3 = add i32 %1, %2
            """,
            }
        )
        T.evaluate(0)

    return string_annotation_of_special_chars


def pointer_type():
    @T.prim_func
    def func_with_ptr_type_annotations(x: T.Ptr[T.int32], y: T.Ptr[T.int32, "shared"]):
        xx = T.allocate([16], "int32", "global")
        yy = T.allocate([16], "int32", "shared")
        a: T.Ptr[T.int32] = T.address_of(xx[0], dtype="handle")
        b: T.Ptr[T.int32, "shared"] = T.address_of(yy[0], dtype="handle")
        T.evaluate(T.call_extern("copy", a, b, dtype=""))

    return func_with_ptr_type_annotations


def buffer_axis_separator():
    @T.prim_func
    def element_wise(a: T.handle, c: T.handle) -> None:
        A = T.match_buffer(a, (128, 128), "float32", axis_separators=[1])
        C = T.match_buffer(c, (128, 128), "float32")
        B = T.alloc_buffer((128, 128), "float32", axis_separators=[1])

        for i, j in T.grid(128, 128):
            with T.block("B"):
                vi, vj = T.axis.remap("SS", [i, j])
                B[vi, vj] = A[vi, vj] * T.float32(2)
        for i, j in T.grid(128, 128):
            with T.block("C"):
                vi, vj = T.axis.remap("SS", [i, j])
                C[vi, vj] = B[vi, vj] + T.float32(1)

    return element_wise


def buffer_ramp_access_as_slice_index():
    @T.prim_func
    def buffer_ramp_access(a: T.handle, b: T.handle, c: T.handle) -> None:
        A = T.match_buffer(a, (128,), "float32")
        B = T.match_buffer(b, (128,), "float32")
        C = T.match_buffer(c, (128,), "float32")
        for i in range(128):
            A[i : i + 1 : 1] = i
        for i in range(4):
            B[i * 32 : i * 32 + 32] = A[i * 32 : i * 32 + 32 : 1] + T.broadcast(1.0, 32)
        for i in range(4):
            C[i : i + 128 : 4] = B[i : i + 128 : 4] + T.broadcast(1.0, 32)

    return buffer_ramp_access


def let_expression():
    @T.prim_func
    def func():
        x = T.var("int32")
        T.evaluate(T.let(x, 1, x + 1))

    return func


def void_ptr():
    @T.prim_func
    def func(out_ret_value: T.Ptr[T.void]):
        T.evaluate(out_ret_value)

    return func


def decl_buffer():
    @T.prim_func
    def func(A: T.Buffer[(16, 16), "float32"], B: T.Buffer[(16, 16), "float32"]) -> None:
        A_flattened = T.decl_buffer(data=A.data, shape=(256,), dtype="float32")
        B_flattened = T.decl_buffer(data=B.data, shape=(256,), dtype="float32")
        C_alias = T.decl_buffer(data=A_flattened.data, shape=(256,), dtype="float32")
        for i in range(256):
            B_flattened[i] = A_flattened[i] + C_alias[i] + T.float32(1.0)

    return func


ir_generator = tvm.testing.parameter(
    opt_gemm_normalize,
    opt_gemm_lower,
    opt_gemm_mod_host,
    opt_conv_tensorcore_normalize,
    opt_conv_tensorcore_lower,
    opt_conv_tensorcore_mod_host,
    vthread_func,
    matmul,
    module_const,
    constant,
    rank0,
    rank0_block,
    select,
    minmax,
    abs,
    constant_folding,
    simplify_bracket,
    while_loop,
    primfunc_with_allocate_annotations,
    comm_reducer_single_reduce_group,
    comm_reducer_multiple_reduce_groups,
    multiple_commreducer,
    loop_extent_dependent,
    nontrivial_range_axis,
    func_with_target_spec_by_config,
    func_with_target_spec_by_str,
    func_root_attr,
    func_T_ptr_let_statement,
    func_T_ptr_allocate,
    llvm_intrin_call,
    parse_bufferslice_as_range_bound,
    int64_support,
    string_annotation_escaping,
    pointer_type,
    buffer_axis_separator,
    buffer_ramp_access_as_slice_index,
    let_expression,
    void_ptr,
    decl_buffer,
)


def test_roundtrip(ir_generator):
    original = ir_generator()
    after_roundtrip = tvm.script.from_source(original.script(show_meta=True))
    tvm.ir.assert_structural_equal(original, after_roundtrip, True)


if __name__ == "__main__":
    tvm.testing.main()
