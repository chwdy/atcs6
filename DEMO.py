"""
Railway System Demo

This is the main demonstration script for the railway management system.
It imports all components from separate modules and creates a comprehensive
demo showing train dispatching, scheduling, and safety systems in action.
"""

from datetime import datetime, timedelta
from enums import Direction, SignalState, SwitchPosition
from network import RailwayNetwork
from train import Train, TrainSchedule, ScheduleStop
from interlocking import InterlockingRoute
from dispatcher import TrainDispatcher

# ============= Demo Functions =============
    STRAIGHT = "straight"
    CURVE = "curve"
    SWITCH = "switch"
    SIGNAL = "signal"
    STATION = "station"
    BLOCK_BOUNDARY = "block_boundary"  # 闭塞分区边界

class Direction(Enum):
    NORTH = (0, 1)
    EAST = (1, 0)
    SOUTH = (0, -1)
    WEST = (-1, 0)
    
    def opposite(self):
        return {
            Direction.NORTH: Direction.SOUTH,
            Direction.SOUTH: Direction.NORTH,
            Direction.EAST: Direction.WEST,
            Direction.WEST: Direction.EAST
        }[self]

class SignalState(Enum):
    GREEN = "green"        # 允许通过
    RED = "red"           # 禁止通过
    YELLOW = "yellow"     # 注意，准备停车
    DOUBLE_YELLOW = "double_yellow"  # 下一个信号可能是红灯

class SwitchPosition(Enum):
    MAIN = "main"
    DIVERGING = "diverging"
    LOCKED = "locked"  # 锁定状态，不可切换

class BlockState(Enum):
    FREE = "free"          # 空闲
    OCCUPIED = "occupied"  # 占用
    RESERVED = "reserved"  # 预约
    LOCKED = "locked"      # 锁定（用于联锁）

class TrainStatus(Enum):
    IDLE = "idle"
    RUNNING = "running"
    WAITING = "waiting"
    AT_STATION = "at_station"
    DELAYED = "delayed"

# ============= 闭塞分区系统 =============

@dataclass
class BlockSection:
    """闭塞分区 - 同一时间只能有一列车"""
    block_id: str
    tracks: List[Tuple[int, int]] = field(default_factory=list)
    state: BlockState = BlockState.FREE
    occupied_by: Optional[str] = None  # 列车ID
    entry_signal: Optional[Tuple[int, int]] = None  # 入口信号机位置
    exit_signal: Optional[Tuple[int, int]] = None   # 出口信号机位置
    
    def can_enter(self, train_id: str) -> bool:
        """检查列车是否可以进入此分区"""
        return (self.state == BlockState.FREE or 
                (self.state == BlockState.RESERVED and self.occupied_by == train_id))
    
    def reserve(self, train_id: str) -> bool:
        """预约分区"""
        if self.state == BlockState.FREE:
            self.state = BlockState.RESERVED
            self.occupied_by = train_id
            return True
        return False
    
    def occupy(self, train_id: str) -> bool:
        """占用分区"""
        if self.can_enter(train_id):
            self.state = BlockState.OCCUPIED
            self.occupied_by = train_id
            return True
        return False
    
    def release(self):
        """释放分区"""
        self.state = BlockState.FREE
        self.occupied_by = None

# ============= 联锁系统 =============

@dataclass
class InterlockingRoute:
    """联锁进路"""
    route_id: str
    start_signal: Tuple[int, int]
    end_signal: Tuple[int, int]
    path: List[Tuple[int, int]]  # 进路包含的轨道
    required_switch_positions: Dict[Tuple[int, int], SwitchPosition]  # 需要的道岔位置
    conflicting_routes: Set[str] = field(default_factory=set)  # 冲突的进路
    is_locked: bool = False
    locked_by: Optional[str] = None  # 锁定该进路的列车ID
    
    def can_lock(self, train_id: str) -> bool:
        """检查是否可以锁定此进路"""
        if self.is_locked and self.locked_by != train_id:
            return False
        return True
    
    def lock(self, train_id: str) -> bool:
        """锁定进路"""
        if self.can_lock(train_id):
            self.is_locked = True
            self.locked_by = train_id
            return True
        return False
    
    def unlock(self):
        """解锁进路"""
        self.is_locked = False
        self.locked_by = None

class InterlockingSystem:
    """联锁系统 - 确保进路安全"""
    def __init__(self):
        self.routes: Dict[str, InterlockingRoute] = {}
    
    def add_route(self, route: InterlockingRoute):
        """添加进路"""
        self.routes[route.route_id] = route
    
    def set_route_conflict(self, route_id1: str, route_id2: str):
        """设置两条进路互相冲突"""
        if route_id1 in self.routes and route_id2 in self.routes:
            self.routes[route_id1].conflicting_routes.add(route_id2)
            self.routes[route_id2].conflicting_routes.add(route_id1)
    
    def request_route(self, route_id: str, train_id: str, 
                     network: 'RailwayNetwork') -> bool:
        """请求锁定一条进路"""
        route = self.routes.get(route_id)
        if not route:
            return False
        
        # 检查冲突进路
        for conflict_id in route.conflicting_routes:
            conflict_route = self.routes[conflict_id]
            if conflict_route.is_locked:
                print(f"  ✗ 进路 {route_id} 被冲突进路 {conflict_id} 阻挡")
                return False
        
        # 检查并设置道岔位置
        for switch_pos, required_position in route.required_switch_positions.items():
            switch_track = network.get_track(*switch_pos)
            if switch_track and switch_track.track_type == TrackType.SWITCH:
                if switch_track.switch_position == SwitchPosition.LOCKED:
                    print(f"  ✗ 道岔 {switch_pos} 被锁定")
                    return False
                switch_track.switch_position = required_position
                switch_track.reserved_by = train_id
        
        # 锁定进路
        if route.lock(train_id):
            # 设置信号灯
            start_signal = network.get_track(*route.start_signal)
            if start_signal:
                start_signal.signal_state = SignalState.GREEN
            
            print(f"  ✓ 进路 {route_id} 已锁定给列车 {train_id}")
            return True
        
        return False
    
    def release_route(self, route_id: str, network: 'RailwayNetwork'):
        """释放进路"""
        route = self.routes.get(route_id)
        if not route:
            return
        
        # 解锁道岔
        for switch_pos in route.required_switch_positions.keys():
            switch_track = network.get_track(*switch_pos)
            if switch_track:
                switch_track.reserved_by = None
        
        # 设置信号灯为红色
        start_signal = network.get_track(*route.start_signal)
        if start_signal:
            start_signal.signal_state = SignalState.RED
        
        route.unlock()
        print(f"  进路 {route_id} 已释放")

# ============= 时刻表系统 =============

@dataclass
class ScheduleStop:
    """时刻表停靠站"""
    station_pos: Tuple[int, int]
    station_name: str
    arrival_time: datetime
    departure_time: datetime
    platform: Optional[str] = None

@dataclass
class TrainSchedule:
    """列车时刻表"""
    train_id: str
    schedule_id: str
    stops: List[ScheduleStop] = field(default_factory=list)
    start_time: datetime = None
    end_time: datetime = None
    priority: int = 0  # 优先级（越大越高）
    
    def get_current_target(self, current_time: datetime) -> Optional[ScheduleStop]:
        """获取当前目标站点"""
        for stop in self.stops:
            if current_time < stop.arrival_time:
                return stop
        return None
    
    def is_delayed(self, current_time: datetime, current_pos: Tuple[int, int]) -> bool:
        """检查是否晚点"""
        target = self.get_current_target(current_time)
        if target and target.station_pos == current_pos:
            return current_time > target.arrival_time
        return False
    
    def get_delay_minutes(self, current_time: datetime, 
                         current_target: ScheduleStop) -> float:
        """获取晚点分钟数"""
        if current_time > current_target.arrival_time:
            delay = current_time - current_target.arrival_time
            return delay.total_seconds() / 60
        return 0

# ============= 核心数据结构 =============

@dataclass
class TrackSegment:
    """轨道段"""
    x: int
    y: int
    track_type: TrackType
    length: float = 1.0
    connections: dict = None  # Dict[Direction, List[Direction]] - maps entry_direction -> list of possible exit_directions
    curve_radius: Optional[float] = None
    
    # 道岔
    switch_position: Optional[SwitchPosition] = None
    main_route: Optional[Tuple[Direction, Direction]] = None
    diverging_route: Optional[Tuple[Direction, Direction]] = None
    
    # 信号灯
    signal_state: Optional[SignalState] = None
    signal_direction: Optional[Direction] = None
    
    # 占用状态
    occupied_by: Optional[str] = None
    reserved_by: Optional[str] = None
    
    # 所属闭塞分区
    block_section_id: Optional[str] = None
    
    def __post_init__(self):
        if self.connections is None:
            self.connections = {}

@dataclass
class Train:
    """列车"""
    train_id: str
    length: float
    min_curve_radius: float
    max_speed: float = 100.0  # 最大速度 km/h
    current_speed: float = 0.0
    acceleration: float = 2.0  # 加速度 m/s²
    
    # 位置信息
    current_pos: Optional[Tuple[int, int]] = None
    current_direction: Optional[Direction] = None
    occupied_tracks: List[Tuple[int, int]] = field(default_factory=list)
    
    # 状态
    status: TrainStatus = TrainStatus.IDLE
    
    # 时刻表
    schedule: Optional[TrainSchedule] = None
    
    # 当前路径
    current_path: Optional[List[Tuple[int, int, Direction]]] = None
    path_index: int = 0
    
    # 预约的闭塞分区
    reserved_blocks: List[str] = field(default_factory=list)

# ============= 铁路网络 =============

class RailwayNetwork:
    def __init__(self):
        self.tracks: Dict[Tuple[int, int], TrackSegment] = {}
        self.trains: Dict[str, Train] = {}
        self.block_sections: Dict[str, BlockSection] = {}
        self.interlocking: InterlockingSystem = InterlockingSystem()
        self.schedules: Dict[str, TrainSchedule] = {}
        
    def add_track(self, track: TrackSegment):
        """添加轨道段"""
        self.tracks[(track.x, track.y)] = track
    
    def add_straight_track(self, x: int, y: int, direction: Direction, 
                          block_id: Optional[str] = None):
        """添加直线轨道
        
        Args:
            direction: The track orientation (e.g., EAST for horizontal track)
        """
        track = TrackSegment(x, y, TrackType.STRAIGHT)
        opposite = direction.opposite()
        # For straight tracks: entry_direction -> [exit_direction]
        # If train enters from WEST (moving EAST), it exits EAST
        # If train enters from EAST (moving WEST), it exits WEST
        track.connections = {
            opposite: [direction],  # Enter from opposite of orientation, exit in orientation
            direction: [opposite]   # Enter from orientation direction, exit in opposite
        }
        track.block_section_id = block_id
        self.add_track(track)
    
    def add_curve_track(self, x: int, y: int, entry_dir: Direction, 
                       exit_dir: Direction, radius: float,
                       block_id: Optional[str] = None):
        """添加弯道"""
        track = TrackSegment(
            x, y, TrackType.CURVE, 
            curve_radius=radius,
            block_section_id=block_id
        )
        track.connections = {
            entry_dir: [exit_dir],
            exit_dir: [entry_dir]
        }
        self.add_track(track)
    
    def add_switch(self, x: int, y: int, 
                   entry_dir: Direction,
                   main_exit: Direction, 
                   diverging_exit: Direction,
                   block_id: Optional[str] = None):
        """添加道岔"""
        track = TrackSegment(
            x, y, TrackType.SWITCH,
            switch_position=SwitchPosition.MAIN,
            block_section_id=block_id
        )
        
        track.main_route = (entry_dir, main_exit)
        track.diverging_route = (entry_dir, diverging_exit)
        
        track.connections = {
            entry_dir: [main_exit, diverging_exit],
            main_exit.opposite(): [entry_dir.opposite()],
            diverging_exit.opposite(): [entry_dir.opposite()]
        }
        
        self.add_track(track)
    
    def add_signal(self, x: int, y: int, direction: Direction, 
                   state: SignalState = SignalState.RED,
                   block_id: Optional[str] = None):
        """添加信号机
        
        Args:
            direction: The direction the signal faces (trains moving in this direction see the signal)
        """
        track = TrackSegment(
            x, y, TrackType.SIGNAL,
            signal_state=state,
            signal_direction=direction,
            block_section_id=block_id
        )
        
        opposite = direction.opposite()
        # Signal acts like a straight track for connections
        track.connections = {
            opposite: [direction],  # Enter from opposite, exit in signal direction
            direction: [opposite]   # Enter from signal direction, exit in opposite
        }
        self.add_track(track)
    
    def add_station(self, x: int, y: int, direction: Direction,
                   station_name: str, block_id: Optional[str] = None):
        """添加车站
        
        Args:
            direction: The orientation of the station platform
        """
        track = TrackSegment(
            x, y, TrackType.STATION,
            block_section_id=block_id
        )
        track.station_name = station_name
        
        opposite = direction.opposite()
        # Station acts like a straight track for connections  
        track.connections = {
            opposite: [direction],  # Enter from opposite, exit in direction
            direction: [opposite]   # Enter from direction, exit in opposite
        }
        self.add_track(track)
    
    def create_block_section(self, block_id: str, 
                           track_positions: List[Tuple[int, int]],
                           entry_signal: Tuple[int, int],
                           exit_signal: Tuple[int, int]):
        """创建闭塞分区"""
        block = BlockSection(
            block_id=block_id,
            tracks=track_positions,
            entry_signal=entry_signal,
            exit_signal=exit_signal
        )
        self.block_sections[block_id] = block
        
        # 设置轨道的闭塞分区ID
        for pos in track_positions:
            track = self.get_track(*pos)
            if track:
                track.block_section_id = block_id
        
        return block
    
    def get_track(self, x: int, y: int) -> Optional[TrackSegment]:
        return self.tracks.get((x, y))
    
    def is_track_available(self, x: int, y: int, train_id: str) -> bool:
        """检查轨道是否可用"""
        track = self.get_track(x, y)
        if not track:
            return False
        
        if track.occupied_by and track.occupied_by != train_id:
            return False
        if track.reserved_by and track.reserved_by != train_id:
            return False
        
        return True
    
    def get_block_section(self, track_pos: Tuple[int, int]) -> Optional[BlockSection]:
        """获取轨道所属的闭塞分区"""
        track = self.get_track(*track_pos)
        if track and track.block_section_id:
            return self.block_sections.get(track.block_section_id)
        return None
    
    def visualization(self):
        """简单的文本可视化"""
        min_x = min(pos[0] for pos in self.tracks.keys())
        max_x = max(pos[0] for pos in self.tracks.keys())
        min_y = min(pos[1] for pos in self.tracks.keys())
        max_y = max(pos[1] for pos in self.tracks.keys())
        
        for y in range(max_y, min_y - 1, -1):
            row = ""
            for x in range(min_x, max_x + 1):
                track = self.get_track(x, y)
                if track:
                    if track.track_type == TrackType.STRAIGHT:
                        row += "─"
                    elif track.track_type == TrackType.CURVE:
                        row += "╭"  # 简化表示
                    elif track.track_type == TrackType.SWITCH:
                        row += "S"
                    elif track.track_type == TrackType.SIGNAL:
                        row += "⚑"
                    elif track.track_type == TrackType.STATION:
                        row += "▦"
                    else:
                        row += "?"
                else:
                    row += " "
            print(row)

# ============= 动态调度系统 =============

class TrainDispatcher:
    """列车调度系统"""
    def __init__(self, network: RailwayNetwork):
        self.network = network
        self.pathfinder = RailwayPathfinder(network)
        self.current_time = datetime.now()
        
        # 冲突检测
        self.conflict_matrix: Dict[str, Set[str]] = defaultdict(set)
    
    def update_time(self, new_time: datetime):
        """更新系统时间"""
        self.current_time = new_time
    
    def schedule_train(self, train: Train, schedule: TrainSchedule) -> bool:
        """为列车安排时刻表"""
        train.schedule = schedule
        self.network.schedules[schedule.schedule_id] = schedule
        
        print(f"\n列车 {train.train_id} 时刻表:")
        for i, stop in enumerate(schedule.stops):
            print(f"  站点 {i+1}: {stop.station_name}")
            print(f"    到达: {stop.arrival_time.strftime('%H:%M:%S')}")
            print(f"    出发: {stop.departure_time.strftime('%H:%M:%S')}")
        
        return True
    
    def dispatch_train(self, train: Train, target_pos: Tuple[int, int]) -> bool:
        """调度列车前往目标"""
        if not train.current_pos:
            print(f"✗ 列车 {train.train_id} 没有当前位置")
            return False
        
        print(f"\n调度列车 {train.train_id}")
        print(f"  当前位置: {train.current_pos}")
        print(f"  目标位置: {target_pos}")
        
        # 寻找路径
        path = self.pathfinder.find_path(
            train,
            start_pos=train.current_pos,
            goal_pos=target_pos,
            start_direction=train.current_direction or Direction.EAST
        )
        
        if not path:
            print(f"  ✗ 未找到路径")
            return False
        
        print(f"  ✓ 找到路径，长度: {len(path)} 段")
        
        # 检查并预约闭塞分区
        if not self._reserve_blocks_for_path(train, path):
            print(f"  ✗ 无法预约所需的闭塞分区")
            return False
        
        # 检查并锁定联锁进路
        if not self._lock_interlocking_routes(train, path):
            print(f"  ✗ 无法锁定联锁进路")
            self._release_blocks(train)
            return False
        
        train.current_path = path
        train.path_index = 0
        train.status = TrainStatus.RUNNING
        
        print(f"  ✓ 列车调度成功")
        return True
    
    def _reserve_blocks_for_path(self, train: Train, 
                                 path: List[Tuple[int, int, Direction]]) -> bool:
        """为路径预约闭塞分区"""
        required_blocks = set()
        
        # 收集路径上所有的闭塞分区
        for x, y, _ in path:
            block = self.network.get_block_section((x, y))
            if block:
                required_blocks.add(block.block_id)
        
        print(f"  需要预约闭塞分区: {required_blocks}")
        
        # 尝试预约所有分区
        reserved = []
        for block_id in required_blocks:
            block = self.network.block_sections[block_id]
            if not block.reserve(train.train_id):
                print(f"    ✗ 闭塞分区 {block_id} 预约失败 (状态: {block.state.value})")
                # 回滚已预约的分区
                for prev_block_id in reserved:
                    self.network.block_sections[prev_block_id].release()
                return False
            reserved.append(block_id)
            print(f"    ✓ 闭塞分区 {block_id} 预约成功")
        
        train.reserved_blocks = reserved
        return True
    
    def _release_blocks(self, train: Train):
        """释放列车预约的闭塞分区"""
        for block_id in train.reserved_blocks:
            block = self.network.block_sections.get(block_id)
            if block:
                block.release()
        train.reserved_blocks.clear()
    
    def _lock_interlocking_routes(self, train: Train,
                                  path: List[Tuple[int, int, Direction]]) -> bool:
        """锁定路径上的联锁进路"""
        # 简化版：检查路径上的信号机和道岔
        for x, y, exit_dir in path:
            track = self.network.get_track(x, y)
            
            # 如果是道岔，确保位置正确
            if track and track.track_type == TrackType.SWITCH:
                # 这里需要根据路径确定道岔应该的位置
                # 简化处理：假设已经在寻路时设置好了
                pass
        
        return True
    
    def move_train_one_step(self, train: Train) -> bool:
        """移动列车一步"""
        if not train.current_path or train.status != TrainStatus.RUNNING:
            return False
        
        if train.path_index >= len(train.current_path):
            # 到达目的地
            train.status = TrainStatus.AT_STATION
            self._release_blocks(train)
            print(f"列车 {train.train_id} 到达目的地")
            return False
        
        x, y, exit_dir = train.current_path[train.path_index]
        
        # 检查信号灯
        track = self.network.get_track(x, y)
        if track and track.track_type == TrackType.SIGNAL:
            if track.signal_state == SignalState.RED:
                print(f"列车 {train.train_id} 在 ({x},{y}) 等待红灯")
                train.status = TrainStatus.WAITING
                return False
        
        # 占用新轨道
        block = self.network.get_block_section((x, y))
        if block:
            block.occupy(train.train_id)
        
        # 释放旧轨道
        if train.current_pos:
            old_block = self.network.get_block_section(train.current_pos)
            if old_block and old_block.block_id not in train.reserved_blocks[1:]:
                old_block.release()
        
        train.current_pos = (x, y)
        train.current_direction = exit_dir
        train.path_index += 1
        
        return True
    
    def check_schedule_conflicts(self) -> List[Tuple[str, str]]:
        """检查时刻表冲突"""
        conflicts = []
        schedules = list(self.network.schedules.values())
        
        for i in range(len(schedules)):
            for j in range(i + 1, len(schedules)):
                if self._schedules_conflict(schedules[i], schedules[j]):
                    conflicts.append((schedules[i].train_id, schedules[j].train_id))
        
        return conflicts
    
    def _schedules_conflict(self, schedule1: TrainSchedule, 
                           schedule2: TrainSchedule) -> bool:
        """检查两个时刻表是否冲突"""
        # 简化版：检查是否有相同车站的时间重叠
        for stop1 in schedule1.stops:
            for stop2 in schedule2.stops:
                if stop1.station_pos == stop2.station_pos:
                    # 检查时间重叠
                    if (stop1.arrival_time <= stop2.departure_time and
                        stop1.departure_time >= stop2.arrival_time):
                        return True
        return False
    
    def get_train_status_report(self) -> str:
        """获取所有列车状态报告"""
        report = ["\n" + "=" * 70]
        report.append("列车状态报告")
        report.append("=" * 70)
        
        for train in self.network.trains.values():
            report.append(f"\n列车 {train.train_id}:")
            report.append(f"  状态: {train.status.value}")
            report.append(f"  位置: {train.current_pos}")
            report.append(f"  方向: {train.current_direction.name if train.current_direction else 'N/A'}")
            
            if train.schedule:
                target = train.schedule.get_current_target(self.current_time)
                if target:
                    report.append(f"  下一站: {target.station_name}")
                    report.append(f"  计划到达: {target.arrival_time.strftime('%H:%M:%S')}")
                    
                    if train.schedule.is_delayed(self.current_time, train.current_pos):
                        delay = train.schedule.get_delay_minutes(self.current_time, target)
                        report.append(f"  ⚠ 晚点: {delay:.1f} 分钟")
        
        report.append("\n" + "=" * 70)
        return "\n".join(report)

# ============= 寻路算法 =============

@dataclass
class RailPathNode:
    x: int
    y: int
    entry_direction: Direction  # Direction from which the train enters this track (opposite of movement)
    g_cost: float
    h_cost: float
    parent: Optional['RailPathNode'] = None
    occupied_segments: List[Tuple[int, int]] = None
    
    @property
    def f_cost(self):
        return self.g_cost + self.h_cost
    
    def __lt__(self, other):
        return self.f_cost < other.f_cost
    
    def __hash__(self):
        return hash((self.x, self.y, self.entry_direction))
    
    def __eq__(self, other):
        return (self.x == other.x and self.y == other.y and 
                self.entry_direction == other.entry_direction)
    
    def __post_init__(self):
        if self.occupied_segments is None:
            self.occupied_segments = []
    
    @property 
    def movement_direction(self) -> Direction:
        """The direction the train is moving (opposite of entry_direction)"""
        return self.entry_direction.opposite()

class RailwayPathfinder:
    def __init__(self, network: RailwayNetwork):
        self.network = network
    
    def find_path(self, train: Train, 
                  start_pos: Tuple[int, int], 
                  goal_pos: Tuple[int, int],
                  start_direction: Direction) -> Optional[List[Tuple[int, int, Direction]]]:
        """为列车寻找路径
        
        Args:
            start_direction: The direction the train is currently moving/heading
        """
        start_track = self.network.get_track(*start_pos)
        goal_track = self.network.get_track(*goal_pos)
        
        if not start_track or not goal_track:
            return None
        
        open_set = []
        closed_set = set()
        
        # Convert train's movement direction to track entry direction
        # If train is heading EAST, it enters the starting track from WEST
        start_entry_direction = start_direction.opposite()
        
        start_node = RailPathNode(
            start_pos[0], start_pos[1],
            entry_direction=start_entry_direction,
            g_cost=0,
            h_cost=self._heuristic(start_pos, goal_pos)
        )
        
        heapq.heappush(open_set, start_node)
        
        while open_set:
            print("start with open set:", open_set)
            print("closed set:", closed_set)
            current = heapq.heappop(open_set)
            
            if (current.x, current.y) == goal_pos:
                return self._reconstruct_path(current)
            
            node_key = (current.x, current.y, current.entry_direction)
            if node_key in closed_set:
                continue
            
            closed_set.add(node_key)
            
            neighbors = self._get_valid_neighbors(current, train)
            print("valid neighbors:", neighbors)
            for neighbor_pos, movement_direction, move_cost in neighbors:
                nx, ny = neighbor_pos
                # Convert movement direction to entry direction for the neighbor track
                neighbor_entry_dir = movement_direction.opposite()
                
                neighbor_node = RailPathNode(
                    nx, ny,
                    entry_direction=neighbor_entry_dir,
                    g_cost=current.g_cost + move_cost,
                    h_cost=self._heuristic(neighbor_pos, goal_pos),
                    parent=current
                )
                
                neighbor_key = (nx, ny, neighbor_entry_dir)
                if neighbor_key not in closed_set:
                    heapq.heappush(open_set, neighbor_node)
        
        return None
    
    def _get_valid_neighbors(self, current: RailPathNode, train: Train) -> List[Tuple[Tuple[int, int], Direction, float]]:
        """获取有效的邻居轨道段
        
        Returns:
            List of (neighbor_position, movement_direction, cost) tuples
            where movement_direction is the direction the train moves to reach the neighbor
        """
        current_track = self.network.get_track(current.x, current.y)
        if not current_track:
            return []
        
        neighbors = []
        # Get possible exit directions from current track
        # Use the movement direction (opposite of entry_direction) to determine exits
        current_movement_dir = current.movement_direction
        possible_exits = self._get_exit_directions(current_track, current_movement_dir)
        print(f"From ({current.x},{current.y}) moving {current_movement_dir.name}, possible exits: {[d.name for d in possible_exits]}")
        
        for exit_dir in possible_exits:
            dx, dy = exit_dir.value
            next_x, next_y = current.x + dx, current.y + dy
            next_track = self.network.get_track(next_x, next_y)
            
            if not next_track:
                continue
            
            if not self._check_all_constraints(
                current_track, next_track, train, 
                current_movement_dir, exit_dir
            ):
                continue
            
            move_cost = self._calculate_move_cost(current_track, next_track, train)
            neighbors.append(((next_x, next_y), exit_dir, move_cost))
        
        return neighbors
    
    def _get_exit_directions(self, track: TrackSegment, movement_dir: Direction) -> List[Direction]:
        """根据轨道类型和道岔位置，获取可能的离开方向
        
        Args:
            track: Current track segment
            movement_dir: Direction the train is moving (heading)
            
        Returns:
            List of directions the train can exit this track
            
        Note: track.connections maps entry_direction -> [exit_directions]
        where entry_direction is where the train enters FROM (opposite of movement)
        """
        if track.track_type == TrackType.SWITCH:
            # For switches, use the switch routes directly based on movement direction
            if movement_dir == track.main_route[0]:
                # Train moving along main route entry direction
                if track.switch_position == SwitchPosition.MAIN:
                    return [track.main_route[1]]
                else:
                    return [track.diverging_route[1]]
            elif movement_dir == track.main_route[1].opposite():
                # Train moving reverse along main route (from exit to entry)
                return [track.main_route[0].opposite()]
            elif movement_dir == track.diverging_route[1].opposite():
                # Train moving reverse along diverging route (from exit to entry)  
                return [track.diverging_route[0].opposite()]
            else:
                return []
        else:
            # For regular tracks (straight, curve, signal, station), use connections
            # The connections dict maps: entry_direction -> [exit_directions]
            # where entry_direction is where the train comes FROM (opposite of movement)
            entry_dir = movement_dir.opposite()
            return track.connections.get(entry_dir, [])
    
    def _check_all_constraints(self, current_track: TrackSegment, 
                               next_track: TrackSegment, 
                               train: Train,
                               movement_dir: Direction,
                               exit_dir: Direction) -> bool:
        """检查所有约束条件"""
        
        # 轨道占用检查
        if not self.network.is_track_available(next_track.x, next_track.y, train.train_id):
            return False
        
        # 信号灯检查
        if current_track.track_type == TrackType.SIGNAL:
            if current_track.signal_direction == exit_dir:
                if current_track.signal_state == SignalState.RED:
                    return False
        
        # 弯道半径检查
        if next_track.track_type == TrackType.CURVE:
            if next_track.curve_radius < train.min_curve_radius:
                return False
        
        # 道岔连接规则
        if current_track.track_type == TrackType.SWITCH:
            # Check if the movement through the switch is valid
            if movement_dir == current_track.diverging_route[1].opposite():
                if exit_dir != current_track.diverging_route[0].opposite():
                    return False
        
        # 下一个轨道连接检查 - check if next track accepts entry from this direction
        next_movement_dir = exit_dir  # The direction train will be moving on next track
        if next_movement_dir not in next_track.connections:
            return False
        
        return True
    
    def _calculate_move_cost(self, current_track: TrackSegment, 
                            next_track: TrackSegment, train: Train) -> float:
        """计算移动成本"""
        base_cost = next_track.length
        
        if next_track.track_type == TrackType.CURVE:
            base_cost *= 1.5
        
        if next_track.track_type == TrackType.SWITCH:
            base_cost *= 1.3
        
        if current_track.track_type == TrackType.SIGNAL:
            if current_track.signal_state == SignalState.YELLOW:
                base_cost *= 2.0
        
        return base_cost
    
    def _heuristic(self, pos1: Tuple[int, int], pos2: Tuple[int, int]) -> float:
        """启发式函数"""
        return abs(pos1[0] - pos2[0]) + abs(pos1[1] - pos2[1])
    
    def _reconstruct_path(self, node: RailPathNode) -> List[Tuple[int, int, Direction]]:
        """重建路径
        
        Returns:
            List of (x, y, movement_direction) tuples representing the path
            where movement_direction is the direction to move FROM that position
        """
        path = []
        current = node
        
        while current.parent:
            # Calculate the movement direction from parent to current
            dx = current.x - current.parent.x
            dy = current.y - current.parent.y
            
            movement_dir = None
            for direction in Direction:
                if direction.value == (dx, dy):
                    movement_dir = direction
                    break
            
            if movement_dir:
                # Store the parent position and the direction to move from it
                path.append((current.parent.x, current.parent.y, movement_dir))
            
            current = current.parent
        
        path.reverse()
        return path

# ============= 完整演示 =============

def create_comprehensive_demo():
    """创建完整的演示铁路网络"""
    print("\n" + "=" * 70)
    print("创建铁路网络...")
    print("=" * 70)
    
    network = RailwayNetwork()
    
    # 主线: (0,0) -> (10,0)
    for x in range(11):
        block_id = f"BLOCK_MAIN_{x}"
        network.add_straight_track(x, 0, Direction.EAST, block_id)
    
    # 在各个位置添加信号机
    network.add_signal(1, 0, Direction.EAST, SignalState.GREEN, "BLOCK_MAIN_1")
    network.add_signal(4, 0, Direction.EAST, SignalState.GREEN, "BLOCK_MAIN_4")
    network.add_signal(7, 0, Direction.EAST, SignalState.GREEN, "BLOCK_MAIN_7")
    
    # 在(3,0)添加道岔，分出支线
    network.add_switch(
        3, 0,
        entry_dir=Direction.WEST,
        main_exit=Direction.EAST,
        diverging_exit=Direction.NORTH,
    )
    
    # 支线轨道
    for y in range(1, 4):
        block_id = f"BLOCK_BRANCH_{y}"
        network.add_straight_track(3, y, Direction.NORTH, block_id)
    
    # 支线信号机
    network.add_signal(3, 2, Direction.NORTH, SignalState.GREEN, "BLOCK_BRANCH_2")
    
    # 添加车站
    network.add_station(5, 0, Direction.EAST, "中央站", "BLOCK_MAIN_5")
    network.add_station(9, 0, Direction.EAST, "终点站", "BLOCK_MAIN_9")
    network.add_station(3, 3, Direction.NORTH, "支线站", "BLOCK_BRANCH_3")
    
    # 创建闭塞分区
    network.create_block_section("BLOCK_1", [(0,0), (1,0)], (0,0), (1,0))
    network.create_block_section("BLOCK_2", [(2,0), (3,0), (4,0)], (1,0), (4,0))
    network.create_block_section("BLOCK_3", [(5,0), (6,0), (7,0)], (4,0), (7,0))
    network.create_block_section("BLOCK_4", [(8,0), (9,0), (10,0)], (7,0), (10,0))
    network.create_block_section("BLOCK_BRANCH", [(3,1), (3,2), (3,3)], (3,0), (3,3))
    
    # 创建联锁进路
    route1 = InterlockingRoute(
        route_id="ROUTE_MAIN",
        start_signal=(1, 0),
        end_signal=(7, 0),
        path=[(2,0), (3,0), (4,0), (5,0), (6,0), (7,0)],
        required_switch_positions={(3, 0): SwitchPosition.MAIN}
    )
    network.interlocking.add_route(route1)
    
    route2 = InterlockingRoute(
        route_id="ROUTE_BRANCH",
        start_signal=(1, 0),
        end_signal=(3, 3),
        path=[(2,0), (3,0), (3,1), (3,2), (3,3)],
        required_switch_positions={(3, 0): SwitchPosition.DIVERGING}
    )
    network.interlocking.add_route(route2)
    
    # 设置进路冲突
    network.interlocking.set_route_conflict("ROUTE_MAIN", "ROUTE_BRANCH")
    
    print("✓ 网络创建完成")
    print(f"  轨道段数: {len(network.tracks)}")
    print(f"  闭塞分区数: {len(network.block_sections)}")
    print(f"  联锁进路数: {len(network.interlocking.routes)}")
    
    return network

def run_comprehensive_demo():
    """运行完整演示"""
    
    # 创建网络
    network = create_comprehensive_demo()
    
    # 创建调度系统
    dispatcher = TrainDispatcher(network)
    base_time = datetime.now()
    dispatcher.update_time(base_time)
    
    # 创建列车
    print("\n" + "=" * 70)
    print("创建列车...")
    print("=" * 70)
    
    train1 = Train(
        train_id="T001",
        length=3.0,
        min_curve_radius=4.0,
        max_speed=120.0,
        current_pos=(0, 0),
        current_direction=Direction.EAST
    )
    
    train2 = Train(
        train_id="T002",
        length=2.5,
        min_curve_radius=3.0,
        max_speed=100.0,
        current_pos=(0, 0),
        current_direction=Direction.EAST
    )
    
    network.trains[train1.train_id] = train1
    network.trains[train2.train_id] = train2
    
    print(f"✓ 创建列车 {train1.train_id}")
    print(f"✓ 创建列车 {train2.train_id}")
    
    # 创建时刻表
    print("\n" + "=" * 70)
    print("创建时刻表...")
    print("=" * 70)
    
    schedule1 = TrainSchedule(
        train_id="T001",
        schedule_id="SCH001",
        priority=1,
        stops=[
            ScheduleStop(
                station_pos=(5, 0),
                station_name="中央站",
                arrival_time=base_time + timedelta(minutes=10),
                departure_time=base_time + timedelta(minutes=12)
            ),
            ScheduleStop(
                station_pos=(9, 0),
                station_name="终点站",
                arrival_time=base_time + timedelta(minutes=20),
                departure_time=base_time + timedelta(minutes=22)
            )
        ]
    )
    
    schedule2 = TrainSchedule(
        train_id="T002",
        schedule_id="SCH002",
        priority=2,
        stops=[
            ScheduleStop(
                station_pos=(3, 3),
                station_name="支线站",
                arrival_time=base_time + timedelta(minutes=15),
                departure_time=base_time + timedelta(minutes=17)
            )
        ]
    )
    
    dispatcher.schedule_train(train1, schedule1)
    dispatcher.schedule_train(train2, schedule2)
    
    # 检查时刻表冲突
    print("\n" + "=" * 70)
    print("检查时刻表冲突...")
    print("=" * 70)
    
    conflicts = dispatcher.check_schedule_conflicts()
    if conflicts:
        print("⚠ 发现时刻表冲突:")
        for t1, t2 in conflicts:
            print(f"  {t1} <-> {t2}")
    else:
        print("✓ 无时刻表冲突")
    
    # 场景1: 调度T001去中央站（主线）
    print("\n" + "=" * 70)
    print("场景1: 调度 T001 前往中央站（主线）")
    print("=" * 70)
    
    success = dispatcher.dispatch_train(train1, (5, 0))
    
    if success:
        # 请求锁定主线进路
        print("\n请求锁定主线进路...")
        route_locked = network.interlocking.request_route(
            "ROUTE_MAIN", train1.train_id, network
        )
        
        if route_locked:
            # 模拟列车移动
            print("\n模拟列车移动...")
            for step in range(min(5, len(train1.current_path))):
                success = dispatcher.move_train_one_step(train1)
                if success:
                    print(f"  步骤 {step+1}: 列车位置 {train1.current_pos}")
                else:
                    break
    network.visualization()
    # # 场景2: 尝试同时调度T002去支线站（应该被进路冲突阻挡）
    # print("\n" + "=" * 70)
    # print("场景2: 尝试调度 T002 前往支线站（测试进路冲突）")
    # print("=" * 70)
    
    # # 重置T002位置
    # train2.current_pos = (0, 0)
    # train2.current_direction = Direction.EAST
    
    # success2 = dispatcher.dispatch_train(train2, (3, 3))
    
    # if success2:
    #     print("\n请求锁定支线进路...")
    #     route_locked2 = network.interlocking.request_route(
    #         "ROUTE_BRANCH", train2.train_id, network
    #     )
        
    #     if not route_locked2:
    #         print("  符合预期：进路被冲突阻挡")
    
    # # 场景3: 释放T001的进路后，再调度T002
    # print("\n" + "=" * 70)
    # print("场景3: 释放 T001 进路后，调度 T002")
    # print("=" * 70)
    
    # # 释放T001的进路
    # print("\n释放 T001 的进路和闭塞分区...")
    # network.interlocking.release_route("ROUTE_MAIN", network)
    # dispatcher._release_blocks(train1)
    
    # # 现在T002应该可以获得进路
    # print("\n再次尝试调度 T002...")
    # success2 = dispatcher.dispatch_train(train2, (3, 3))
    
    # if success2:
    #     print("\n请求锁定支线进路...")
    #     route_locked2 = network.interlocking.request_route(
    #         "ROUTE_BRANCH", train2.train_id, network
    #     )
        
    #     if route_locked2:
    #         print("  ✓ 成功锁定支线进路")
            
    #         # 模拟T002移动
    #         print("\n模拟 T002 移动...")
    #         for step in range(min(5, len(train2.current_path))):
    #             success = dispatcher.move_train_one_step(train2)
    #             if success:
    #                 print(f"  步骤 {step+1}: 列车位置 {train2.current_pos}")
    #             else:
    #                 break
    
    # # 显示最终状态报告
    # print(dispatcher.get_train_status_report())
    
    # # 闭塞分区状态
    # print("\n" + "=" * 70)
    # print("闭塞分区状态")
    # print("=" * 70)
    
    # for block_id, block in network.block_sections.items():
    #     print(f"\n{block_id}:")
    #     print(f"  状态: {block.state.value}")
    #     if block.occupied_by:
    #         print(f"  占用者: {block.occupied_by}")
    
    # print("\n" + "=" * 70)
    # print("演示完成")
    # print("=" * 70)

if __name__ == "__main__":
    run_comprehensive_demo()