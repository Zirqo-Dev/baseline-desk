// SPDX-License-Identifier: MIT
// BL-45 cost meter probe. Read-only by construction: this code never exists on chain. tools/costs.py places it at a
// made-up address with an eth_call state override (code + the quote token's balance slot), runs ONE swap round trip
// inside that eth_call and reads the numbers back. Nothing is broadcast; no key, wallet or allowance exists anywhere.
// Pools are paid by plain token transfers: the v3-style swap callback transfers what the pool asks for, and the
// solidly v2 pool gets its input by transfer before swap(). Rebuild: docs/NOTES.md "BL-45" (solcjs 0.8.26, pinned).
pragma solidity 0.8.26;

interface IV3Pool {
    function swap(address recipient, bool zeroForOne, int256 amountSpecified, uint160 sqrtPriceLimitX96, bytes calldata data)
        external returns (int256 amount0, int256 amount1);
    function token0() external view returns (address);
}

interface IV2Pool {
    function swap(uint256 amount0Out, uint256 amount1Out, address to, bytes calldata data) external;
    function getAmountOut(uint256 amountIn, address tokenIn) external view returns (uint256);
    function token0() external view returns (address);
}

contract CostProbe {
    uint8 constant KIND_V3 = 1; // Uniswap v3, PancakeSwap v3, Aerodrome Slipstream (CL): swap() + callback
    uint8 constant KIND_V2 = 2; // Aerodrome v2 volatile (solidly): transfer in, then swap()

    uint160 constant MIN_SQRT = 4295128740;
    uint160 constant MAX_SQRT = 1461446703485210103287273052203988822378723970341;

    // stage codes returned by trip()
    uint8 constant OK = 0;
    uint8 constant NO_MID = 1;
    uint8 constant REVERT_BUY = 2;
    uint8 constant REVERT_SELL = 3;
    uint8 constant ZERO_OUT = 4;

    function _bal(address token, address who) internal view returns (uint256) {
        (bool ok, bytes memory r) = token.staticcall(abi.encodeWithSelector(0x70a08231, who));
        require(ok && r.length >= 32, "bal");
        return abi.decode(r, (uint256));
    }

    function _send(address token, address to, uint256 amount) internal {
        (bool ok, bytes memory r) = token.call(abi.encodeWithSelector(0xa9059cbb, to, amount));
        require(ok && (r.length == 0 || abi.decode(r, (bool))), "send");
    }

    // mid state of one pool: v3-style = (sqrtPriceX96, 0), v2 = (reserve0, reserve1); (0, 0) when unreadable.
    function mid(address pool, uint8 kind) public view returns (uint256 a, uint256 b) {
        bytes4 sel = kind == KIND_V2 ? bytes4(0x0902f1ac) : bytes4(0x3850c7bd); // getReserves() : slot0()
        (bool ok, bytes memory r) = pool.staticcall(abi.encodePacked(sel));
        if (!ok || r.length < 64) return (0, 0);
        (a, b) = abi.decode(r, (uint256, uint256));
        if (kind != KIND_V2) b = 0;
    }

    function mids(address[] calldata pools, uint8[] calldata kinds) external view returns (uint256[] memory out) {
        out = new uint256[](pools.length * 2);
        for (uint256 i = 0; i < pools.length; i++) {
            (out[2 * i], out[2 * i + 1]) = mid(pools[i], kinds[i]);
        }
    }

    // One swap, only callable by this probe itself (so trip() can catch its revert). Returns what the pool's own
    // math says it sends out, and how much of the input actually arrived at the pool (v2; v3 pools check it).
    function leg(address pool, uint8 kind, address tin, uint256 amount) external returns (uint256 poolOut, uint256 arrived) {
        require(msg.sender == address(this), "self");
        address t0 = IV3Pool(pool).token0();
        if (kind == KIND_V2) {
            uint256 before = _bal(tin, pool);
            _send(tin, pool, amount);
            arrived = _bal(tin, pool) - before;
            poolOut = IV2Pool(pool).getAmountOut(arrived, tin);
            require(poolOut > 0, "zero");
            if (tin == t0) IV2Pool(pool).swap(0, poolOut, address(this), "");
            else IV2Pool(pool).swap(poolOut, 0, address(this), "");
        } else {
            bool zeroForOne = tin == t0;
            (int256 a0, int256 a1) = IV3Pool(pool).swap(
                address(this), zeroForOne, int256(amount), zeroForOne ? MIN_SQRT : MAX_SQRT, abi.encode(tin));
            int256 o = zeroForOne ? a1 : a0;
            int256 i = zeroForOne ? a0 : a1;
            require(o < 0, "zero");
            poolOut = uint256(-o);
            arrived = uint256(i);
        }
    }

    function _pay(int256 d0, int256 d1, bytes calldata data) internal {
        address tin = abi.decode(data, (address));
        _send(tin, msg.sender, uint256(d0 > 0 ? d0 : d1));
    }

    function uniswapV3SwapCallback(int256 d0, int256 d1, bytes calldata data) external { _pay(d0, d1, data); }
    function pancakeV3SwapCallback(int256 d0, int256 d1, bytes calldata data) external { _pay(d0, d1, data); }

    // Buy `amount` of quote token q into `t` on `pool`, then sell everything received back to q.
    // r = [mid0a, mid0b, spent_q, pool_out_t, recv_t, mid1a, mid1b, sent_t, arrived_t, pool_out_q, recv_q]
    function trip(address pool, uint8 kind, address q, address t, uint256 amount)
        external returns (uint8 stage, uint256[11] memory r)
    {
        (r[0], r[1]) = mid(pool, kind);
        if (r[0] == 0) return (NO_MID, r);
        uint256 q0 = _bal(q, address(this));
        uint256 t0 = _bal(t, address(this));
        try this.leg(pool, kind, q, amount) returns (uint256 po, uint256) {
            r[3] = po;
        } catch {
            return (REVERT_BUY, r);
        }
        r[2] = q0 - _bal(q, address(this));
        r[4] = _bal(t, address(this)) - t0;
        if (r[4] == 0) return (ZERO_OUT, r);
        (r[5], r[6]) = mid(pool, kind);
        uint256 q1 = _bal(q, address(this));
        r[7] = r[4];
        try this.leg(pool, kind, t, r[4]) returns (uint256 po, uint256 arr) {
            r[9] = po;
            r[8] = arr;
        } catch {
            return (REVERT_SELL, r);
        }
        r[10] = _bal(q, address(this)) - q1;
        if (r[10] == 0) return (ZERO_OUT, r);
        return (OK, r);
    }
}
