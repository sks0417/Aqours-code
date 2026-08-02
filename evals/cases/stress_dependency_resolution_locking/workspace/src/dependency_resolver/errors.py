class DependencyResolverError(Exception):
    pass


class ValidationError(DependencyResolverError):
    def __init__(self, message: str, *, field: str):
        super().__init__(message)
        self.field = field


class UnknownPackage(DependencyResolverError):
    def __init__(self, package: str):
        super().__init__(f"unknown package: {package}")
        self.package = package


class UnsatisfiedConstraints(DependencyResolverError):
    def __init__(self, package: str, constraints):
        super().__init__(f"unsatisfied constraints for {package}")
        self.package = package
        self.constraints = tuple(constraints)


class DependencyCycle(DependencyResolverError):
    def __init__(self, path):
        super().__init__("dependency cycle: " + " -> ".join(path))
        self.path = tuple(path)


class PackageConflict(DependencyResolverError):
    def __init__(self, package: str, other: str):
        super().__init__(f"package conflict: {package} with {other}")
        self.package, self.other = package, other


class LockError(DependencyResolverError):
    def __init__(self, package: str, reason: str):
        super().__init__(f"lock error for {package}: {reason}")
        self.package, self.reason = package, reason
