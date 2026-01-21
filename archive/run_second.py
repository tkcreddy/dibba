import logging
import functools


def log_to_file():
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            try:
                # Log function call details
                print(f"Calling function: {func.__name__}")
                print(f"Arguments: {args}, {kwargs}")

                # Execute the original function
                result = func(*args, **kwargs)

                # Log the return value
                print(f"Function returned: {result}")

                return result
            except Exception as e:
                # Log any exceptions that occur during function execution
                print(f"Error in function {func.__name__}: {str(e)}")
                raise

        return wrapper
    return decorator

#Example usage
if __name__ == "__main__":

    @log_to_file()
    def add_numbers(a, b):
        return a + b

    # Call the decorated function
    result = add_numbers(3, 5)
    print(f"Final result: {result}")
